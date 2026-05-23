# Secure Neo4j MCP Adapter Implementation Plan

## Goal

Build a secure, multi-workspace Text2Cypher flow for HyreFast.

The LLM should still be allowed to generate useful Cypher for candidate search, ranking, taxonomy lookup, and analysis. However, it must never control tenant scope. Candidate data must always be restricted to the authenticated user's workspace.

Core principle:

```text
Do not remove Text2Cypher. Secure it.
```

Target architecture:

```text
Workspace User
  -> HyreFast Backend / Agent
  -> secure_read_cypher
  -> query validator + workspace_id injector
  -> Neo4j read-only credential
```

```text
Admin User
  -> HyreFast Backend / Agent
  -> admin-only mutation tools
  -> role check
  -> Neo4j admin credential
```

Important project-specific schema detail:

```cypher
(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate)
```

Use `workspace_id`, not `id`, on `Workspace`.

---

## Current Position

The HyreFast graph already has the right tenant boundary:

```cypher
(:Workspace)-[:LISTED_CANDIDATE]->(:Candidate)
(:Candidate)-[:HAS_SKILL]->(:Skill)
```

The agent prompt already tells the LLM to query candidates through `Workspace`.

The deployed MCP adapter currently exposes generic tools:

```text
get_neo4j_schema
read_neo4j_cypher
write_neo4j_cypher  # disabled when NEO4J_READ_ONLY=true
```

This means the system currently relies too much on prompt discipline. A prompt can guide the LLM, but it cannot enforce tenant security.

The missing layer is:

```text
secure_read_cypher = flexible Text2Cypher + hard workspace validation
```

---

## Final Tool Design

Use three main categories of tools:

```text
secure_read_cypher
secure_get_schema
admin mutation tools
```

### 1. `secure_read_cypher`

Used by workspace users.

Allows flexible read queries, but enforces:

```text
Writes are blocked.
Candidate/private data must be workspace-scoped.
Skill taxonomy reads are global.
workspace_id comes from backend/session, not the LLM.
```

### 2. `secure_get_schema`

Used by workspace users.

Returns a filtered, safe schema instead of raw APOC schema.

Workspace users should see only the schema they are allowed to query.

### 3. Admin mutation tools

Used only by admins.

Examples:

```text
add_new_skills
update_skill
delete_skill
admin_write_cypher
```

These tools must not even be registered for normal workspace users.

---

## Phase 1: Add Backend Auth Context

### Objective

The backend must know the authenticated user's:

```text
workspace_id
role
```

The LLM must not supply either value.

### Required backend behavior

In the main HyreFast app, derive workspace and role from authentication/session context:

```python
workspace_id = auth_context["workspace_id"]
user_role = auth_context["role"]
```

Do not accept `workspace_id` from chat text or tool arguments.

Example target flow:

```python
def handle_chat(user_message, auth_context):
    workspace_id = auth_context["workspace_id"]
    user_role = auth_context["role"]

    tools = build_tools(
        workspace_id=workspace_id,
        role=user_role,
    )

    response = agent.invoke(
        {"messages": user_message},
        tools=tools,
    )

    return response
```

### Implementation notes

If the current app does not yet have real login/auth, use a temporary server-side configuration value for development:

```env
HYREFAST_WORKSPACE_ID=ws046
HYREFAST_USER_ROLE=workspace_user
```

This is acceptable for local testing only. The important rule remains: the backend provides the workspace, not the LLM.

---

## Phase 2: Rename/Replace Raw Read Tool

### Objective

Stop exposing raw `read_neo4j_cypher` to workspace users.

Replace it with:

```text
secure_read_cypher
```

### Tool input from LLM

```json
{
  "query": "MATCH ... RETURN ...",
  "params": {}
}
```

### Tool input from backend/session

```json
{
  "workspace_id": "ws046"
}
```

### Final execution params

```python
safe_params = dict(params or {})
safe_params["workspace_id"] = authenticated_workspace_id
```

If the LLM passes `workspace_id` inside `params`, overwrite it.

```python
safe_params["workspace_id"] = workspace_id
```

---

## Phase 3: Implement Write Blocking

### Objective

`secure_read_cypher` must only execute read queries.

### Block these keywords/operations

```python
BLOCKED_KEYWORDS = [
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
```

### Recommended validation

Use two layers:

1. Static keyword validation before execution.
2. Neo4j `EXPLAIN` query-type validation.

Example:

```python
async def is_write_query(query: str, params: dict | None = None) -> bool:
    _, summary, _ = await driver.execute_query(
        query_=f"EXPLAIN {query}",
        parameters_=params or {},
        database_=NEO4J_DATABASE,
    )
    return "w" in (summary.query_type or "")
```

Reject if either check detects a write.

---

## Phase 4: Define Public vs Private Labels

### Global taxonomy labels

These can be queried directly by workspace users:

```text
Skill
Category
Subcategory
Alias
Tag
JobRole
Taxonomy
```

Allowed global taxonomy relationships:

```text
BELONGS_TO_SUBCATEGORY
BELONGS_TO_CATEGORY
IN_TAXONOMY
HAS_ALIAS
HAS_TAG
TYPICAL_FOR_ROLE
PARENT_OF
CHILD_OF
REQUIRES
RELATED_TO
CAN_TRANSFER_TO
```

### Private candidate labels

Queries touching these must be scoped through workspace:

```text
Candidate
Company
Institution
Location
Language
Certification
WorkExperience
Education
Achievement
Publication
```

Review queue labels should be treated as private/admin unless intentionally exposed:

```text
CandidateReviewItem
SkillReviewItem
NormalizationCandidate
SkillNormalizationResolution
```

---

## Phase 5: Enforce Workspace Scope

### Required candidate scope pattern

Any query touching candidate/private labels must include:

```cypher
(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate)
```

Equivalent aliases are fine. For example:

```cypher
MATCH (w:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)
MATCH (c)-[:HAS_SKILL]->(s:Skill)
RETURN c.first_name, c.last_name, s.canonical_name
```

This is allowed.

### Reject unscoped candidate access

Reject:

```cypher
MATCH (c:Candidate)
RETURN c
```

Reject:

```cypher
MATCH (c:Candidate)-[:HAS_SKILL]->(s:Skill)
RETURN c, s
```

Reject:

```cypher
MATCH (w:Workspace)-[:LISTED_CANDIDATE]->(c:Candidate)
RETURN w, c
```

Reason: it can see all workspaces because it does not bind `Workspace` to `$workspace_id`.

### Allow global taxonomy reads

Allow:

```cypher
MATCH (s:Skill)-[:BELONGS_TO_SUBCATEGORY]->(sub:Subcategory)
RETURN s.canonical_name, sub.name
LIMIT 20
```

Reason: taxonomy is global and not tenant-private.

---

## Phase 6: Implement `secure_read_cypher` Skeleton

Use this as the starting shape inside the MCP adapter:

```python
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

BLOCKED_KEYWORDS = [
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


def _normalized_query(query: str) -> str:
    return " ".join(query.upper().split())


def _touches_private_label(query: str) -> bool:
    upper = query.upper()
    return any(f":{label.upper()}" in upper for label in PRIVATE_LABELS)


def _has_workspace_scope(query: str) -> bool:
    upper = _normalized_query(query)
    return (
        ":WORKSPACE" in upper
        and "WORKSPACE_ID" in upper
        and "$WORKSPACE_ID" in upper
        and "LISTED_CANDIDATE" in upper
        and ":CANDIDATE" in upper
    )


def _validate_secure_read_query(query: str) -> None:
    upper = _normalized_query(query)

    for keyword in BLOCKED_KEYWORDS:
        if keyword in upper:
            raise ToolError(f"Blocked Cypher operation in read-only tool: {keyword}")

    if _touches_private_label(query) and not _has_workspace_scope(query):
        raise ToolError(
            "Private candidate queries must be scoped through "
            "(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate)."
        )
```

Tool handler shape:

```python
@mcp.tool(
    name=namespace_prefix + "secure_read_cypher",
    title="Secure Read Neo4j Cypher",
    description=(
        "Run a read-only Cypher query. Candidate/private data must be scoped through "
        "(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate). "
        "Never provide workspace_id yourself; the backend injects it. "
        "Global Skill taxonomy can be queried directly."
    ),
)
async def secure_read_cypher(
    query: str,
    params: dict[str, Any] = Field(default_factory=dict),
) -> CallToolResult:
    _validate_secure_read_query(query)

    safe_params = dict(params or {})
    safe_params["workspace_id"] = AUTHENTICATED_WORKSPACE_ID

    if await _is_write_query(query, safe_params):
        raise ToolError("Only read queries are allowed.")

    rows = await driver.execute_query(
        Query(query, timeout=float(NEO4J_READ_TIMEOUT)),
        parameters_=safe_params,
        database_=NEO4J_DATABASE,
        result_transformer_=lambda result: result.data(),
    )

    return _text_result(json.dumps(rows, default=str))
```

Important: `AUTHENTICATED_WORKSPACE_ID` is a placeholder. In production it should come from request/session/auth context, not from the LLM.

---

## Phase 7: Decide How Workspace ID Reaches the Deployed Adapter

There are two viable options.

### Option A: Backend-side MCP proxy/wrapper

The HyreFast backend registers a local tool named `secure_read_cypher`.

That local tool:

1. Receives LLM query and params.
2. Injects backend-authenticated `workspace_id`.
3. Calls the deployed Neo4j MCP adapter.
4. Returns the result.

Pros:

```text
Easiest to integrate with app auth.
No need for per-request auth parsing inside the deployed MCP adapter.
Keeps tenant logic close to the application.
```

Cons:

```text
Raw deployed adapter must not be exposed to untrusted users.
```

### Option B: Adapter-side auth context

The deployed adapter reads `workspace_id` and `role` from a signed token or trusted headers.

Example headers:

```text
Authorization: Bearer <token>
X-Hyrefast-Workspace-Id: ws046
X-Hyrefast-Role: workspace_user
```

Only use headers if they are set by trusted infrastructure, not directly by browsers or users.

Pros:

```text
Security enforcement lives directly with Neo4j access.
Cleaner long-term MCP boundary.
```

Cons:

```text
Requires trusted auth/token plumbing into MCP requests.
May need changes to the MCP client transport.
```

Recommended sequence:

```text
Start with Option A for fast implementation.
Move to Option B when auth infrastructure is stable.
```

---

## Phase 8: Replace Raw Schema Tool with Safe Schema

### Current problem

`get_neo4j_schema` uses APOC metadata sampling and can expose labels/properties that normal workspace users should not need.

### Create `secure_get_schema`

For workspace users, return a fixed safe schema:

```text
(:Workspace)-[:LISTED_CANDIDATE]->(:Candidate)
(:Candidate)-[:HAS_SKILL]->(:Skill)
(:Candidate)-[:CURRENTLY_LOCATED_IN]->(:Location)
(:Candidate)-[:SPEAKS]->(:Language)
(:Candidate)-[:HAS_CERTIFICATION]->(:Certification)
(:Candidate)-[:HAS_EXPERIENCE]->(:WorkExperience)
(:Candidate)-[:HAS_EDUCATION]->(:Education)
(:Candidate)-[:HAS_ACHIEVEMENT]->(:Achievement)
(:Candidate)-[:AUTHORED]->(:Publication)

(:Skill)-[:BELONGS_TO_SUBCATEGORY]->(:Subcategory)
(:Subcategory)-[:BELONGS_TO_CATEGORY]->(:Category)
(:Category)-[:IN_TAXONOMY]->(:Taxonomy)
(:Skill)-[:HAS_ALIAS]->(:Alias)
(:Skill)-[:HAS_TAG]->(:Tag)
(:Skill)-[:TYPICAL_FOR_ROLE]->(:JobRole)
(:Skill)-[:RELATED_TO]->(:Skill)
(:Skill)-[:CAN_TRANSFER_TO]->(:Skill)
(:Skill)-[:PARENT_OF]->(:Skill)
(:Skill)-[:CHILD_OF]->(:Skill)
(:Skill)-[:REQUIRES]->(:Skill)
```

Also include allowed properties:

```text
Candidate:
  first_name, middle_name, last_name, primary_email, resume_url, linkedin_url,
  portfolio_urls, remote_preference, employment_type_pref, shift_preference,
  notice_period_days, work_authorization, visa_sponsorship_needed,
  work_experience_snapshot, education_snapshot, certifications_snapshot,
  skills_snapshot

HAS_SKILL:
  raw_skill, confidence, human_review_needed, normalization_decision

Skill:
  id, canonical_name, display_name, name, aliases, tags, category, subcategory,
  description, typical_roles, is_language, is_software

Category:
  slug, name

Subcategory:
  slug, name
```

Do not expose internal/admin-only labels to workspace users.

Keep full `get_neo4j_schema` only for admin users, or remove it from workspace tool registration.

---

## Phase 9: Update Agent Tool Prompt

Update the HyreFast agent prompt to refer to the new tool names:

Replace:

```text
Use read-cypher for normal search...
Use get-schema...
```

With:

```text
Use secure_read_cypher for normal search, ranking, counting, and graph inspection.
Use secure_get_schema only if the provided schema is not enough.
```

Add this rule:

```text
When querying Candidate or candidate-side data, always start from:
MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)

Never invent or provide workspace_id. The backend injects it.
Do not query Candidate directly.
Skill taxonomy nodes are global and can be queried directly.
```

Update query templates to use:

```cypher
MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)
```

Do not use a literal placeholder like:

```text
<requesting-workspace-id>
```

The query should contain `$workspace_id`.

---

## Phase 10: Gate Admin Mutation Tools

### Current issue

The local custom mutation MCP server exposes:

```text
add_new_skills
update_skill
delete_skill
```

The prompt says to use them only when asked, but prompt policy is not enough for production.

### Required behavior

Only register mutation tools when:

```python
user_role == "admin"
```

Example:

```python
tools = []

tools.append(secure_read_cypher)
tools.append(secure_get_schema)

if user_role == "admin":
    tools.append(add_new_skills)
    tools.append(update_skill)
    tools.append(delete_skill)
    tools.append(admin_write_cypher)
```

For workspace users, mutation tools should not appear in the available MCP tool list.

---

## Phase 11: Use Separate Neo4j Credentials

Create separate Neo4j users/credentials:

```text
Workspace read credential:
  read-only
  used by secure_read_cypher

Admin credential:
  write/admin privileges
  used only by admin mutation tools
```

Environment example:

```env
NEO4J_READ_URI=bolt://...
NEO4J_READ_USERNAME=hyrefast_reader
NEO4J_READ_PASSWORD=...

NEO4J_ADMIN_URI=bolt://...
NEO4J_ADMIN_USERNAME=hyrefast_admin
NEO4J_ADMIN_PASSWORD=...
```

Even if the query validator has a bug, read-only credentials reduce blast radius.

---

## Phase 12: Add Tests

Create focused tests for the validator.

### Should pass

Global taxonomy:

```cypher
MATCH (s:Skill)-[:BELONGS_TO_SUBCATEGORY]->(sub:Subcategory)
RETURN s.canonical_name, sub.name
LIMIT 10
```

Workspace-scoped candidate query:

```cypher
MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)-[r:HAS_SKILL]->(s:Skill)
RETURN c.primary_email, s.canonical_name
LIMIT 10
```

Workspace-scoped profile query:

```cypher
MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)
OPTIONAL MATCH (c)-[:HAS_EXPERIENCE]->(job:WorkExperience)
RETURN c.primary_email, collect(job.job_title)
LIMIT 10
```

### Should fail

Unscoped candidate:

```cypher
MATCH (c:Candidate)
RETURN c
```

All workspaces:

```cypher
MATCH (w:Workspace)-[:LISTED_CANDIDATE]->(c:Candidate)
RETURN w, c
```

Write:

```cypher
MATCH (c:Candidate)
SET c.test = true
RETURN c
```

APOC:

```cypher
CALL apoc.meta.schema()
YIELD value
RETURN value
```

Delete:

```cypher
MATCH (c:Candidate)
DETACH DELETE c
```

### Test dimensions

Test at least:

```text
Keyword blocking
Private label detection
Workspace pattern detection
workspace_id param overwrite
Global taxonomy allowlist
Neo4j EXPLAIN write detection
```

---

## Phase 13: Add Logging and Auditing

Log every secure query execution with safe metadata:

```text
tool_name
workspace_id
role
query_hash
query_classification
blocked_or_allowed
row_count
duration_ms
```

Do not log full candidate result payloads in production logs.

For blocked queries, log:

```text
reason
workspace_id
query_hash
```

Avoid logging secrets or raw access tokens.

---

## Phase 14: Deployment Steps

1. Implement `secure_read_cypher` in the MCP adapter or in a backend wrapper.
2. Implement `secure_get_schema`.
3. Keep old `read_neo4j_cypher` disabled or unregister it for workspace users.
4. Keep `NEO4J_READ_ONLY=true` for the deployed workspace adapter.
5. Configure read-only Neo4j credentials.
6. Update HyreFast agent prompt to use `secure_read_cypher`.
7. Pass backend-authenticated `workspace_id` into the secure tool layer.
8. Gate mutation tools by role.
9. Run validator tests.
10. Run end-to-end recruiter queries.
11. Test blocked malicious or accidental queries.
12. Deploy to staging.
13. Check logs for blocked/allowed query behavior.
14. Deploy to production.

---

## End-to-End Verification Queries

### Candidate search should work

Ask the agent:

```text
Find candidates with Python and Neo4j skills.
```

Expected Cypher shape:

```cypher
MATCH (:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(c:Candidate)-[r:HAS_SKILL]->(s:Skill)
...
RETURN ...
```

Expected result:

```text
Only candidates listed in the authenticated workspace.
```

### Global taxonomy should work

Ask:

```text
Show skill categories related to cloud computing.
```

Expected:

```text
Allowed even without Workspace, as long as it only touches taxonomy labels.
```

### Unscoped candidate query should fail

Force or test:

```cypher
MATCH (c:Candidate)
RETURN c
```

Expected:

```text
Rejected by secure_read_cypher.
```

### Cross-workspace query should fail

Force or test:

```cypher
MATCH (w:Workspace)-[:LISTED_CANDIDATE]->(c:Candidate)
RETURN w.workspace_id, c.primary_email
```

Expected:

```text
Rejected because Workspace is not bound to $workspace_id.
```

---

## Immediate Checklist

```text
[ ] Decide whether secure wrapper lives in HyreFast backend or deployed MCP adapter.
[ ] Add authenticated workspace_id and role to backend request context.
[ ] Create secure_read_cypher.
[ ] Overwrite params["workspace_id"] from backend/session.
[ ] Block write keywords and unsafe CALLs.
[ ] Keep Neo4j EXPLAIN write detection.
[ ] Define global taxonomy labels.
[ ] Define private candidate labels.
[ ] Reject private-label queries without Workspace -> LISTED_CANDIDATE -> Candidate scope.
[ ] Create secure_get_schema with fixed safe schema.
[ ] Stop exposing raw read_neo4j_cypher to workspace users.
[ ] Stop exposing raw get_neo4j_schema to workspace users.
[ ] Register mutation tools only for admins.
[ ] Use read-only Neo4j credentials for workspace read path.
[ ] Use admin Neo4j credentials only for admin tools.
[ ] Update agent prompt and query templates to use $workspace_id.
[ ] Add validator unit tests.
[ ] Add end-to-end tests for allowed and blocked queries.
[ ] Deploy to staging.
[ ] Verify Cloud Run/reverse proxy auth.
[ ] Rotate any credentials that have been pasted into chats/logs.
```

---

## Most Important Rule

```text
The LLM may generate Cypher, but it must never control tenant scope.
```

The backend owns `workspace_id`.

The secure tool injects `workspace_id`.

Neo4j queries touching candidate data must always flow through:

```cypher
(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate)
```
