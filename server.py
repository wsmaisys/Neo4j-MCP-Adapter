import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from neo4j import AsyncGraphDatabase, Query
from neo4j.exceptions import ClientError, Neo4jError
from pydantic import Field

try:
    import tiktoken
except ImportError:  # pragma: no cover - optional dependency for token truncation
    tiktoken = None

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))


logger = logging.getLogger("mcp_neo4j_cypher")
logger.setLevel(logging.INFO)


# Exceptions and small parsing helpers keep configuration and tool errors consistent.
class ToolError(Exception):
    pass


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        return default


def _env_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None:
        return None

    try:
        return int(value)
    except ValueError:
        return None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return default


def _split_csv(value: str | None, default: list[str]) -> list[str]:
    if value is None:
        return default

    items = [item.strip() for item in value.split(",") if item.strip()]
    return items if items else default


def _value_sanitize(value: Any, list_limit: int = 128) -> Any:
    # Keep nested payloads shallow enough for MCP responses and token limits.
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, dict):
                nested = _value_sanitize(item)
                if nested is not None:
                    sanitized[key] = nested
            elif isinstance(item, list):
                if len(item) < list_limit:
                    nested = _value_sanitize(item)
                    if nested is not None:
                        sanitized[key] = nested
            else:
                sanitized[key] = item
        return sanitized
    if isinstance(value, list):
        if len(value) < list_limit:
            return [_value_sanitize(item) for item in value if _value_sanitize(item) is not None]
        return None
    return value


def _truncate_string_to_tokens(text: str, token_limit: int, model: str = "gpt-4") -> str:
    # If tiktoken is missing, return the original text instead of failing the server.
    if tiktoken is None:
        return text

    encoding = tiktoken.encoding_for_model(model)
    tokens = encoding.encode(text)
    if len(tokens) > token_limit:
        tokens = tokens[:token_limit]
    return encoding.decode(tokens)


def _format_namespace(namespace: str) -> str:
    if not namespace:
        return ""
    # Tool names use a trailing dash separator when a namespace is configured.
    return namespace if namespace.endswith("-") else f"{namespace}-"


def _normalize_mount_path(path: str) -> str:
    # Accept /mcp and /mcp/ from config, but store one canonical form everywhere.
    normalized = (path or "/mcp").strip()
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    normalized = normalized.rstrip("/")
    return normalized or "/"


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    # Strip noisy metadata while preserving the fields clients usually care about.
    cleaned: dict[str, Any] = {}

    for key, entry in schema.items():
        new_entry: dict[str, Any] = {"type": entry["type"]}

        if "count" in entry:
            new_entry["count"] = entry["count"]

        labels = entry.get("labels", [])
        if labels:
            new_entry["labels"] = labels

        props = entry.get("properties", {})
        clean_props: dict[str, Any] = {}
        for prop_name, prop_info in props.items():
            clean_prop: dict[str, Any] = {}
            if "indexed" in prop_info:
                clean_prop["indexed"] = prop_info["indexed"]
            if "type" in prop_info:
                clean_prop["type"] = prop_info["type"]
            if clean_prop:
                clean_props[prop_name] = clean_prop

        if clean_props:
            new_entry["properties"] = clean_props

        if entry.get("relationships"):
            rels_out: dict[str, Any] = {}
            for rel_name, rel in entry["relationships"].items():
                clean_rel: dict[str, Any] = {}

                if "direction" in rel:
                    clean_rel["direction"] = rel["direction"]

                rel_labels = rel.get("labels", [])
                if rel_labels:
                    clean_rel["labels"] = rel_labels

                rel_props = rel.get("properties", {})
                clean_rel_props: dict[str, Any] = {}
                for rel_prop_name, rel_prop_info in rel_props.items():
                    clean_rel_prop: dict[str, Any] = {}
                    if "indexed" in rel_prop_info:
                        clean_rel_prop["indexed"] = rel_prop_info["indexed"]
                    if "type" in rel_prop_info:
                        clean_rel_prop["type"] = rel_prop_info["type"]
                    if clean_rel_prop:
                        clean_rel_props[rel_prop_name] = clean_rel_prop

                if clean_rel_props:
                    clean_rel["properties"] = clean_rel_props

                if clean_rel:
                    rels_out[rel_name] = clean_rel

            if rels_out:
                new_entry["relationships"] = rels_out

        cleaned[key] = new_entry

    return cleaned


def _log_tool_start(tool_name: str, extra: str = "") -> None:
    if extra:
        logger.info("Running `%s` %s", tool_name, extra)
    else:
        logger.info("Running `%s`", tool_name)


def _maybe_tool(enabled: bool, *args: Any, **kwargs: Any):
    def decorator(func):
        if enabled:
            return mcp.tool(*args, **kwargs)(func)
        return func

    return decorator


def _normalized_query(query: str) -> str:
    return " ".join(query.upper().split())


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword).replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![A-Z0-9_]){escaped}(?![A-Z0-9_])", re.IGNORECASE)


def _touches_private_label(query: str) -> bool:
    return any(
        re.search(rf":\s*{re.escape(label)}\b", query, flags=re.IGNORECASE)
        for label in PRIVATE_LABELS
    )


def _candidate_labels(query: str) -> list[str | None]:
    matches = re.finditer(
        r"\(\s*([A-Za-z_][A-Za-z0-9_]*)?\s*:\s*Candidate\b",
        query,
        flags=re.IGNORECASE,
    )
    return [match.group(1) for match in matches]


def _workspace_scoped_candidate_variable(query: str) -> str | None:
    pattern = re.compile(
        r"\(\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*)?:\s*Workspace\s*"
        r"\{[^}]*\bworkspace_id\s*:\s*\$workspace_id\b[^}]*\}\s*\)"
        r"\s*-\s*\[\s*(?:[A-Za-z_][A-Za-z0-9_]*\s*)?:?\s*LISTED_CANDIDATE\s*\]\s*->\s*"
        r"\(\s*([A-Za-z_][A-Za-z0-9_]*)?\s*:\s*Candidate\b",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(query)
    return match.group(1) if match else None


def _has_workspace_scope(query: str) -> bool:
    return _workspace_scoped_candidate_variable(query) is not None


def _validate_secure_read_query(query: str) -> None:
    for keyword in BLOCKED_READ_KEYWORDS:
        if _keyword_pattern(keyword).search(query):
            raise ToolError(f"Blocked Cypher operation in read-only tool: {keyword}")

    if not _touches_private_label(query):
        return

    scoped_candidate = _workspace_scoped_candidate_variable(query)
    if scoped_candidate is None:
        raise ToolError(
            "Private candidate queries must be scoped through "
            "(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate)."
        )

    for candidate_var in _candidate_labels(query):
        if candidate_var is None:
            raise ToolError(
                "Private candidate queries must use a named Candidate variable in the "
                "workspace-scoped MATCH pattern."
            )
        if candidate_var != scoped_candidate:
            raise ToolError(
                "All Candidate labels in a secure read query must use the same Candidate "
                "variable that is scoped through Workspace."
            )


def _requires_workspace_scope(query: str) -> bool:
    return _touches_private_label(query) or "$workspace_id" in query.lower()


def _resolve_workspace_id(params: dict[str, Any]) -> str:
    if HYREFAST_WORKSPACE_ID:
        return HYREFAST_WORKSPACE_ID

    if HYREFAST_ALLOW_WORKSPACE_ID_PARAM:
        value = params.get("workspace_id")
        if isinstance(value, str) and value.strip():
            return value.strip()

    raise ToolError(
        "No workspace_id is configured for secure_read_cypher. Set HYREFAST_WORKSPACE_ID, "
        "or enable HYREFAST_ALLOW_WORKSPACE_ID_PARAM=true for temporary POC testing."
    )


# Runtime configuration is resolved up front so the server fails fast on bad env state.
NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")
NEO4J_NAMESPACE = os.getenv("NEO4J_NAMESPACE", "")
NEO4J_TRANSPORT = os.getenv("NEO4J_TRANSPORT", "http")
NEO4J_MCP_SERVER_HOST = os.getenv("NEO4J_MCP_SERVER_HOST", "0.0.0.0")
NEO4J_MCP_SERVER_PORT = _env_int("NEO4J_MCP_SERVER_PORT", 8000)
NEO4J_MCP_SERVER_PATH = _normalize_mount_path(os.getenv("NEO4J_MCP_SERVER_PATH", "/mcp"))
NEO4J_MCP_SERVER_ALLOW_ORIGINS = _split_csv(
    os.getenv("NEO4J_MCP_SERVER_ALLOW_ORIGINS"),
    [],
)
NEO4J_MCP_SERVER_ALLOWED_HOSTS = _split_csv(
    os.getenv("NEO4J_MCP_SERVER_ALLOWED_HOSTS"),
    ["*"],
)
NEO4J_MCP_SERVER_STATELESS = _env_bool("NEO4J_MCP_SERVER_STATELESS", True)
NEO4J_READ_ONLY = _env_bool("NEO4J_READ_ONLY", False)
NEO4J_EXPOSE_RAW_TOOLS = _env_bool("NEO4J_EXPOSE_RAW_TOOLS", False)
NEO4J_SCHEMA_SAMPLE_SIZE = _env_int("NEO4J_SCHEMA_SAMPLE_SIZE", 1000)
NEO4J_READ_TIMEOUT = _env_int("NEO4J_READ_TIMEOUT", 30)
NEO4J_RESPONSE_TOKEN_LIMIT = _env_optional_int("NEO4J_RESPONSE_TOKEN_LIMIT")
HYREFAST_WORKSPACE_ID = os.getenv("HYREFAST_WORKSPACE_ID", "").strip()
HYREFAST_ALLOW_WORKSPACE_ID_PARAM = _env_bool("HYREFAST_ALLOW_WORKSPACE_ID_PARAM", False)

# Build the MCP app once so tool registration happens at import time.
mcp = FastMCP("mcp-neo4j-cypher", stateless_http=True)
namespace_prefix = _format_namespace(NEO4J_NAMESPACE)


PRIVATE_LABELS = {
    "Candidate",
    "Company",
    "Institution",
    "Location",
    "Language",
    "Certification",
    "WorkExperience",
    "Education",
    "Achievement",
    "Publication",
    "CandidateReviewItem",
    "SkillReviewItem",
    "NormalizationCandidate",
    "SkillNormalizationResolution",
}

BLOCKED_READ_KEYWORDS = [
    "CREATE",
    "MERGE",
    "SET",
    "DELETE",
    "DETACH",
    "REMOVE",
    "DROP",
    "LOAD CSV",
    "CALL DBMS",
    "CALL APOC",
    "CALL GDS",
]

SAFE_WORKSPACE_SCHEMA = {
    "description": (
        "Workspace users can query global Skill taxonomy directly. Candidate and "
        "candidate-side data must be reached through "
        "(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate)."
    ),
    "relationships": [
        "(:Workspace)-[:LISTED_CANDIDATE]->(:Candidate)",
        "(:Candidate)-[:HAS_SKILL]->(:Skill)",
        "(:Candidate)-[:CURRENTLY_LOCATED_IN]->(:Location)",
        "(:Candidate)-[:SPEAKS]->(:Language)",
        "(:Candidate)-[:HAS_CERTIFICATION]->(:Certification)",
        "(:Candidate)-[:HAS_EXPERIENCE]->(:WorkExperience)",
        "(:Candidate)-[:HAS_EDUCATION]->(:Education)",
        "(:Candidate)-[:HAS_ACHIEVEMENT]->(:Achievement)",
        "(:Candidate)-[:AUTHORED]->(:Publication)",
        "(:Skill)-[:BELONGS_TO_SUBCATEGORY]->(:Subcategory)",
        "(:Subcategory)-[:BELONGS_TO_CATEGORY]->(:Category)",
        "(:Category)-[:IN_TAXONOMY]->(:Taxonomy)",
        "(:Skill)-[:HAS_ALIAS]->(:Alias)",
        "(:Skill)-[:HAS_TAG]->(:Tag)",
        "(:Skill)-[:TYPICAL_FOR_ROLE]->(:JobRole)",
        "(:Skill)-[:RELATED_TO]->(:Skill)",
        "(:Skill)-[:CAN_TRANSFER_TO]->(:Skill)",
        "(:Skill)-[:PARENT_OF]->(:Skill)",
        "(:Skill)-[:CHILD_OF]->(:Skill)",
        "(:Skill)-[:REQUIRES]->(:Skill)",
    ],
    "properties": {
        "Candidate": [
            "first_name",
            "middle_name",
            "last_name",
            "primary_email",
            "resume_url",
            "linkedin_url",
            "portfolio_urls",
            "remote_preference",
            "employment_type_pref",
            "shift_preference",
            "notice_period_days",
            "work_authorization",
            "visa_sponsorship_needed",
            "work_experience_snapshot",
            "education_snapshot",
            "certifications_snapshot",
            "skills_snapshot",
        ],
        "HAS_SKILL": [
            "raw_skill",
            "confidence",
            "human_review_needed",
            "normalization_decision",
        ],
        "Skill": [
            "id",
            "canonical_name",
            "display_name",
            "name",
            "aliases",
            "tags",
            "category",
            "subcategory",
            "description",
            "typical_roles",
            "is_language",
            "is_software",
        ],
        "Category": ["slug", "name"],
        "Subcategory": ["slug", "name"],
    },
}

SECURE_SCHEMA_TOOL_DESCRIPTION = """
Return the safe HyreFast workspace-user schema as JSON text.

Use this tool when you need the allowed labels, relationships, or properties before
writing a Cypher query.

Input contract:
- No arguments.

Important rules:
- This is a schema/help tool only; it does not query candidate rows.
- Candidate and candidate-side data must be queried from Workspace:
  MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)
- Global taxonomy labels can be queried directly: Skill, Category, Subcategory,
  Alias, Tag, JobRole, Taxonomy.
- Do not call raw schema tools for normal workspace-user questions.
""".strip()

SECURE_READ_TOOL_DESCRIPTION = """
Run a secure read-only Cypher query against Neo4j and return rows as JSON text.

Input contract:
- query: string, required. A read-only Cypher query.
- params: object, optional. Query parameters other than workspace_id.

Workspace contract:
- For Candidate or candidate-side data, the query must include this exact scope pattern:
  MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)
- Use $workspace_id in the Cypher. The adapter injects its value.
- Do not invent, request, or hard-code a workspace id in the query.
- Do not put workspace_id in params unless the server is explicitly running temporary
  POC mode with HYREFAST_ALLOW_WORKSPACE_ID_PARAM=true.

Allowed:
- Global taxonomy reads over Skill, Category, Subcategory, Alias, Tag, JobRole,
  Taxonomy, and skill relationship types.
- Workspace-scoped candidate reads.

Rejected:
- Writes or mutations: CREATE, MERGE, SET, DELETE, DETACH, REMOVE, DROP, LOAD CSV.
- Unsafe procedure calls: CALL apoc, CALL dbms, CALL gds.
- Candidate/private queries that do not start from Workspace LISTED_CANDIDATE.
- Queries over all Workspace nodes or all Candidate nodes.

Good candidate query shape:
  MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)-[r:HAS_SKILL]->(s:Skill)
  WHERE toLower(coalesce(s.canonical_name, "")) = $skill
  RETURN c.first_name, c.last_name, c.primary_email, r.confidence, s.canonical_name
  LIMIT 10

Good taxonomy query shape:
  MATCH (s:Skill)-[:BELONGS_TO_SUBCATEGORY]->(sub:Subcategory)
  RETURN s.canonical_name, sub.name
  LIMIT 10
""".strip()

SECURE_READ_QUERY_DESCRIPTION = """
Read-only Cypher query.

For candidate/private data, include:
MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)

For global taxonomy-only questions, Workspace scope is not required.

Never use write clauses or unsafe procedure calls.
""".strip()

SECURE_READ_PARAMS_DESCRIPTION = """
Optional Cypher parameters as a JSON object.

Do not include workspace_id during normal operation; the adapter injects it.
Temporary POC exception: workspace_id may be supplied only when
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=true.
""".strip()


def _require_env(name: str, value: str | None) -> str:
    if value:
        return value
    raise RuntimeError(f"Missing required environment variable: {name}")


NEO4J_URI = _require_env("NEO4J_URI", NEO4J_URI)
NEO4J_USERNAME = _require_env("NEO4J_USERNAME", NEO4J_USERNAME)
NEO4J_PASSWORD = _require_env("NEO4J_PASSWORD", NEO4J_PASSWORD)
NEO4J_DATABASE = _require_env("NEO4J_DATABASE", NEO4J_DATABASE)


driver = AsyncGraphDatabase.driver(
    NEO4J_URI,
    auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
)


def _text_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


async def _is_write_query(query: str, params: dict[str, Any] | None = None) -> bool:
    # EXPLAIN lets us classify the query without executing the mutation.
    _, summary, _ = await driver.execute_query(
        query_=f"EXPLAIN {query}",
        parameters_=params or {},
        database_=NEO4J_DATABASE,
    )

    return "w" in (summary.query_type or "")


def _configure_http_transport() -> None:
    # Keep all transport settings in one place so main() stays focused.
    mcp.settings.host = NEO4J_MCP_SERVER_HOST
    mcp.settings.port = NEO4J_MCP_SERVER_PORT
    mcp.settings.mount_path = NEO4J_MCP_SERVER_PATH
    mcp.settings.stateless_http = NEO4J_MCP_SERVER_STATELESS

    transport_security = mcp.settings.transport_security
    transport_security.allowed_origins = NEO4J_MCP_SERVER_ALLOW_ORIGINS

    allowed_hosts = []
    for host in NEO4J_MCP_SERVER_ALLOWED_HOSTS:
        if host == "*":
            allowed_hosts = ["*"]
            break
        allowed_hosts.append(host)
    transport_security.allowed_hosts = allowed_hosts


# Tool handlers are grouped together below in the same order the client usually reads them.
@mcp.tool(
    name=namespace_prefix + "secure_get_schema",
    title="Secure Get Neo4j Schema",
    description=SECURE_SCHEMA_TOOL_DESCRIPTION,
    annotations=ToolAnnotations(
        title="Secure Get Neo4j Schema",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    meta={
        "tags": ["neo4j", "schema", "read-only", "hyrefast", "secure"],
        "surface": "safe-schema-inspection",
    },
)
async def secure_get_schema() -> CallToolResult:
    """Return the filtered schema allowed for workspace-user Text2Cypher."""
    _log_tool_start("secure_get_schema")
    return _text_result(json.dumps(SAFE_WORKSPACE_SCHEMA, default=str))


@mcp.tool(
    name=namespace_prefix + "secure_read_cypher",
    title="Secure Read Neo4j Cypher",
    description=SECURE_READ_TOOL_DESCRIPTION,
    annotations=ToolAnnotations(
        title="Secure Read Neo4j Cypher",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    meta={
        "tags": ["neo4j", "cypher", "read", "query", "hyrefast", "secure"],
        "surface": "secure-data-retrieval",
    },
)
async def secure_read_cypher(
    query: str = Field(..., description=SECURE_READ_QUERY_DESCRIPTION),
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=SECURE_READ_PARAMS_DESCRIPTION,
    ),
) -> CallToolResult:
    """Execute a workspace-scoped read Cypher query on the Neo4j database."""
    _validate_secure_read_query(query)

    safe_params = dict(params or {})
    workspace_id = None
    if _requires_workspace_scope(query):
        workspace_id = _resolve_workspace_id(safe_params)
        safe_params["workspace_id"] = workspace_id

    if await _is_write_query(query, safe_params):
        raise ToolError("Only read queries are allowed for secure_read_cypher")

    if workspace_id:
        _log_tool_start("secure_read_cypher", f"for workspace {workspace_id}.")
    else:
        _log_tool_start("secure_read_cypher", "for global taxonomy read.")

    try:
        query_obj = Query(query, timeout=float(NEO4J_READ_TIMEOUT))
        rows = await driver.execute_query(
            query_obj,
            parameters_=safe_params,
            database_=NEO4J_DATABASE,
            result_transformer_=lambda result: result.data(),
        )
    except Neo4jError as exc:
        logger.error("Neo4j Error executing secure read query: %s\n%s\n%s", exc, query, safe_params)
        raise ToolError(f"Neo4j Error: {exc}\n{query}\n{safe_params}") from exc
    except Exception as exc:
        logger.error("Error executing secure read query: %s\n%s\n%s", exc, query, safe_params)
        raise ToolError(f"Error: {exc}\n{query}\n{safe_params}") from exc

    sanitized_rows = [_value_sanitize(row) for row in rows]
    results_json_str = json.dumps(sanitized_rows, default=str)
    if NEO4J_RESPONSE_TOKEN_LIMIT:
        results_json_str = _truncate_string_to_tokens(results_json_str, NEO4J_RESPONSE_TOKEN_LIMIT)

    return _text_result(results_json_str)


@_maybe_tool(
    NEO4J_EXPOSE_RAW_TOOLS,
    name=namespace_prefix + "get_neo4j_schema",
    title="Get Neo4j Schema",
    description=(
        "Inspect the Neo4j schema with APOC metadata sampling. Returns a cleaned JSON "
        "view of labels, relationship types, property types, and indexed flags. "
        "Use sample_size only when you need to tune breadth versus performance."
    ),
    annotations=ToolAnnotations(
        title="Get Neo4j Schema",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    meta={
        "tags": ["neo4j", "schema", "read-only", "apoc"],
        "surface": "schema-inspection",
    },
)
async def get_neo4j_schema(
    sample_size: int = Field(
        default=NEO4J_SCHEMA_SAMPLE_SIZE,
        description=(
            "Number of graph elements to sample when inferring the schema. "
            "Use -1 for a full-graph sample. Larger values are slower but more complete."
        ),
    ),
) -> CallToolResult:
    """
    Returns nodes, their properties (with types and indexed flags), and relationships.

    You should only provide a `sample_size` value if requested by the user, or tuning the retrieval performance.

    Performance Notes:
        - If `sample_size` is not provided, uses the server's default sample setting defined in the server configuration.
        - If retrieving the schema times out, try lowering the sample size, e.g. `sample_size=100`.
        - To sample the entire graph use `sample_size=-1`.
    """
    effective_sample_size = sample_size if sample_size else NEO4J_SCHEMA_SAMPLE_SIZE
    _log_tool_start("get_neo4j_schema", f"with sample size {effective_sample_size}.")
    # APOC schema sampling is the narrowest way to inspect the graph shape.
    get_schema_query = f"CALL apoc.meta.schema({{sample: {effective_sample_size}}}) YIELD value RETURN value"

    try:
        results_json = await driver.execute_query(
            query_=get_schema_query,
            result_transformer_=lambda result: result.data(),
            database_=NEO4J_DATABASE,
        )
    except ClientError as exc:
        if "Neo.ClientError.Procedure.ProcedureNotFound" in str(exc):
            raise ToolError(
                "Neo4j Client Error: The schema inspection procedure is not available on this Neo4j instance."
            ) from exc
        raise ToolError(f"Neo4j Client Error: {exc}") from exc
    except Neo4jError as exc:
        raise ToolError(f"Neo4j Error: {exc}") from exc
    except Exception as exc:
        raise ToolError(f"Unexpected Error: {exc}") from exc

    if not results_json:
        return _text_result(json.dumps({}, default=str))

    schema_clean = _clean_schema(results_json[0].get("value", {}))
    return _text_result(json.dumps(schema_clean, default=str))


@_maybe_tool(
    NEO4J_EXPOSE_RAW_TOOLS,
    name=namespace_prefix + "read_neo4j_cypher",
    title="Read Neo4j Cypher",
    description=(
        "Run a read-only Cypher query against Neo4j and return the rows as JSON text. "
        "Provide query plus optional params. Use this for MATCH/RETURN style lookups only."
    ),
    annotations=ToolAnnotations(
        title="Read Neo4j Cypher",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    meta={
        "tags": ["neo4j", "cypher", "read", "query"],
        "surface": "data-retrieval",
    },
)
async def read_neo4j_cypher(
    query: str = Field(..., description="The Cypher query to execute."),
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional Cypher parameters passed as a JSON object.",
    ),
) -> CallToolResult:
    """Execute a read Cypher query on the neo4j database."""
    # Reject writes early so this handler stays read-only by construction.
    if await _is_write_query(query, params):
        raise ToolError("Only MATCH queries are allowed for read-query")

    _log_tool_start("read_neo4j_cypher")

    try:
        query_obj = Query(query, timeout=float(NEO4J_READ_TIMEOUT))
        rows = await driver.execute_query(
            query_obj,
            parameters_=params,
            database_=NEO4J_DATABASE,
            result_transformer_=lambda result: result.data(),
        )
    except Neo4jError as exc:
        logger.error("Neo4j Error executing read query: %s\n%s\n%s", exc, query, params)
        raise ToolError(f"Neo4j Error: {exc}\n{query}\n{params}") from exc
    except Exception as exc:
        logger.error("Error executing read query: %s\n%s\n%s", exc, query, params)
        raise ToolError(f"Error: {exc}\n{query}\n{params}") from exc

    sanitized_rows = [_value_sanitize(row) for row in rows]
    results_json_str = json.dumps(sanitized_rows, default=str)
    if NEO4J_RESPONSE_TOKEN_LIMIT:
        results_json_str = _truncate_string_to_tokens(results_json_str, NEO4J_RESPONSE_TOKEN_LIMIT)

    return _text_result(results_json_str)


if not NEO4J_READ_ONLY:

    @_maybe_tool(
        NEO4J_EXPOSE_RAW_TOOLS,
        name=namespace_prefix + "write_neo4j_cypher",
        title="Write Neo4j Cypher",
        description=(
            "Run a write Cypher query against Neo4j and return the write counters as JSON text. "
            "Provide query plus optional params. Use only when the user explicitly wants mutation."
        ),
        annotations=ToolAnnotations(
            title="Write Neo4j Cypher",
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        ),
        meta={
            "tags": ["neo4j", "cypher", "write", "mutation"],
            "surface": "data-mutation",
        },
    )
    async def write_neo4j_cypher(
        query: str = Field(..., description="The Cypher query to execute."),
        params: dict[str, Any] = Field(
            default_factory=dict,
            description="Optional Cypher parameters passed as a JSON object.",
        ),
    ) -> CallToolResult:
        """Execute a write Cypher query on the neo4j database."""
        # Confirm the query is actually a write before executing it.
        if not await _is_write_query(query, params):
            raise ToolError("Only write queries are allowed for write-query")

        _log_tool_start("write_neo4j_cypher")

        try:
            _, summary, _ = await driver.execute_query(
                query,
                parameters_=params,
                database_=NEO4J_DATABASE,
            )
        except Neo4jError as exc:
            logger.error("Neo4j Error executing write query: %s\n%s\n%s", exc, query, params)
            raise ToolError(f"Neo4j Error: {exc}\n{query}\n{params}") from exc
        except Exception as exc:
            logger.error("Error executing write query: %s\n%s\n%s", exc, query, params)
            raise ToolError(f"Error: {exc}\n{query}\n{params}") from exc

        return _text_result(json.dumps(summary.counters.__dict__, default=str))


def main(
    transport: Literal["streamable-http"] = "streamable-http",
) -> None:
    logger.info("Starting MCP neo4j Server")
    if NEO4J_TRANSPORT not in {"http", "streamable-http"}:
        logger.warning(
            "NEO4J_TRANSPORT=%s is not supported by this adapter; using streamable-http.",
            NEO4J_TRANSPORT,
        )

    _configure_http_transport()
    mcp.run(transport=transport, mount_path=NEO4J_MCP_SERVER_PATH)


if __name__ == "__main__":
    # Allow direct execution without requiring a separate launcher script.
    main()
