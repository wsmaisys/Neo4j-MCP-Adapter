import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional

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
    return namespace if namespace.endswith("-") else f"{namespace}-"


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
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


NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE")
NEO4J_NAMESPACE = os.getenv("NEO4J_NAMESPACE", "")
NEO4J_TRANSPORT = os.getenv("NEO4J_TRANSPORT", "http")
NEO4J_MCP_SERVER_HOST = os.getenv("NEO4J_MCP_SERVER_HOST", "0.0.0.0")
NEO4J_MCP_SERVER_PORT = _env_int("NEO4J_MCP_SERVER_PORT", 8000)
NEO4J_MCP_SERVER_PATH = os.getenv("NEO4J_MCP_SERVER_PATH", "/mcp/")
NEO4J_MCP_SERVER_ALLOW_ORIGINS = _split_csv(
    os.getenv("NEO4J_MCP_SERVER_ALLOW_ORIGINS"),
    [],
)
NEO4J_MCP_SERVER_ALLOWED_HOSTS = _split_csv(
    os.getenv("NEO4J_MCP_SERVER_ALLOWED_HOSTS"),
    [],
)
NEO4J_MCP_SERVER_STATELESS = _env_bool("NEO4J_MCP_SERVER_STATELESS", True)
NEO4J_READ_ONLY = _env_bool("NEO4J_READ_ONLY", False)
NEO4J_SCHEMA_SAMPLE_SIZE = _env_int("NEO4J_SCHEMA_SAMPLE_SIZE", 1000)
NEO4J_READ_TIMEOUT = _env_int("NEO4J_READ_TIMEOUT", 30)
NEO4J_RESPONSE_TOKEN_LIMIT = _env_optional_int("NEO4J_RESPONSE_TOKEN_LIMIT")

mcp = FastMCP("mcp-neo4j-cypher", stateless_http=True)
namespace_prefix = _format_namespace(NEO4J_NAMESPACE)


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
    _, summary, _ = await driver.execute_query(
        query_=f"EXPLAIN {query}",
        parameters_=params or {},
        database_=NEO4J_DATABASE,
    )

    return "w" in (summary.query_type or "")


def create_mcp_server() -> FastMCP:
    return mcp


def _configure_http_transport() -> None:
    mcp.settings.host = NEO4J_MCP_SERVER_HOST
    mcp.settings.port = NEO4J_MCP_SERVER_PORT
    mcp.settings.mount_path = NEO4J_MCP_SERVER_PATH
    mcp.settings.stateless_http = NEO4J_MCP_SERVER_STATELESS

    transport_security = mcp.settings.transport_security
    transport_security.allowed_origins = NEO4J_MCP_SERVER_ALLOW_ORIGINS

    allowed_hosts = []
    for host in NEO4J_MCP_SERVER_ALLOWED_HOSTS:
        if ":" in host or host.startswith("["):
            allowed_hosts.append(host)
        else:
            allowed_hosts.append(f"{host}:*")
    transport_security.allowed_hosts = allowed_hosts


@mcp.tool(
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


@mcp.tool(
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


@mcp.tool(
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
    if NEO4J_READ_ONLY:
        raise ToolError("Write queries are disabled in read-only mode")

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
    main()
