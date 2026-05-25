# Neo4j MCP Adapter Environment And Deployment Guide

This guide documents the configuration read by `server.py` and the companion
`mcp_client.py`, explains why each value exists, and gives deployment recipes
for the HyreFast workspace-user and administrator flows.

## What This Service Does

The adapter exposes Neo4j access as MCP tools:

| Tool | Audience | Purpose | Security behavior |
| --- | --- | --- | --- |
| `secure_get_schema` | Workspace users and admins | Returns a fixed safe schema view | Does not expose private/admin-only schema |
| `secure_read_cypher` | Workspace users and optionally admins | Runs tenant-scoped candidate reads and global taxonomy reads | Enforces `workspace_id` scope for workspace/private data |
| `admin_get_schema` | Admin only, through the Agent wrapper | Returns the full sampled Neo4j schema | Requires `HYREFAST_ADMIN_READ_TOKEN` |
| `admin_read_cypher` | Admin only, through the Agent wrapper | Runs read-only cross-workspace reports and candidate reads | Requires `HYREFAST_ADMIN_READ_TOKEN`; blocks writes |
| `get_neo4j_schema` | Debug only | Raw full-schema inspection | Registered only when raw tools are enabled |
| `read_neo4j_cypher` | Debug only | Raw read Cypher | Registered only when raw tools are enabled |
| `write_neo4j_cypher` | Debug only | Raw writes | Requires raw tools enabled and read-only mode disabled |

In the supported chat architecture, browsers call the Agent service. The Agent
connects to this adapter and injects the server-to-server token for admin MCP
tools. A browser or LLM should never provide `HYREFAST_ADMIN_READ_TOKEN`.

## Server Environment Variables

### Neo4j Connection

These values are required by `server.py`. The service fails during startup if
any one is missing.

| Variable | Required | Example | Need / utility |
| --- | --- | --- | --- |
| `NEO4J_URI` | Yes | `bolt://neo4j-host:7687` | Bolt connection address of the Neo4j database queried by tools. |
| `NEO4J_USERNAME` | Yes | `hyrefast_reader` | Neo4j account used by the adapter driver. Use the least-privileged account appropriate for exposed tools. |
| `NEO4J_PASSWORD` | Yes | `<secret>` | Password for `NEO4J_USERNAME`. Store in deployment secrets, not source control. |
| `NEO4J_DATABASE` | Yes | `neo4j` | Neo4j database name passed on every query. |

Important: the current adapter uses one Neo4j driver for secure and
authenticated admin reads. When `NEO4J_READ_ONLY=true`, keep this Neo4j
account read-only; admin MCP access means broader visibility, not database
write privileges.

### Tool Naming And Transport

| Variable | Required | Default | Need / utility |
| --- | --- | --- | --- |
| `NEO4J_NAMESPACE` | No | Empty | Prefixes tool names, for example `prod-admin_read_cypher`. Leave empty unless multiple adapters must coexist in one client. |
| `NEO4J_TRANSPORT` | No | `http` | Selects server transport. The implementation supports HTTP/streamable HTTP and falls back to `streamable-http` for unsupported values. |

### MCP HTTP Server

| Variable | Required | Default | Need / utility |
| --- | --- | --- | --- |
| `NEO4J_MCP_SERVER_HOST` | No | `0.0.0.0` | Address FastMCP listens on. Use `0.0.0.0` in containers; use `127.0.0.1` for local-only use if desired. |
| `NEO4J_MCP_SERVER_PORT` | No | `8000` | Listener port inside the process/container. The repository Docker setup maps port `8080`, so configure this as `8080` there. |
| `NEO4J_MCP_SERVER_PATH` | No | `/mcp` | MCP mount path. Agent URLs must end in the same path. |
| `NEO4J_MCP_SERVER_ALLOW_ORIGINS` | No | Empty | Comma-separated allowed browser origins for transport security. Browser chat normally calls the Agent, not this adapter, so it can normally remain empty. |
| `NEO4J_MCP_SERVER_ALLOWED_HOSTS` | No | `*` | Comma-separated request Host allowlist. Set the deployed hostname in Cloud Run or local hostnames for local testing. |
| `NEO4J_MCP_SERVER_STATELESS` | No | `true` | Configures stateless HTTP behavior for MCP requests. Keep `true` for the deployed adapter. |

### Exposure And Query Limits

| Variable | Required | Default | Need / utility |
| --- | --- | --- | --- |
| `NEO4J_READ_ONLY` | No | `false` | When `true`, prevents registration of raw `write_neo4j_cypher`. Keep `true` for the HyreFast Agent adapter. |
| `NEO4J_EXPOSE_RAW_TOOLS` | No | `false` | Registers raw schema/read/write tools for debugging when `true`. Keep `false` for any service reachable by the Agent or users. |
| `NEO4J_SCHEMA_SAMPLE_SIZE` | No | `1000` | Default APOC metadata sampling size used by `admin_get_schema` and raw schema inspection. Increase only if missing schema details matter more than cost/latency. |
| `NEO4J_READ_TIMEOUT` | No | `30` | Maximum query runtime in seconds for read tools. Protects the service from expensive generated queries. |
| `NEO4J_RESPONSE_TOKEN_LIMIT` | No | Empty | Truncates serialized tool results when set to an integer and optional tokenization support is installed. Useful to control model context size. |

### Workspace Isolation And Admin Authentication

| Variable | Required | Default | Need / utility |
| --- | --- | --- | --- |
| `HYREFAST_WORKSPACE_ID` | Mode-dependent | Empty | Fixed trusted workspace injected into `secure_read_cypher`. Set this for a dedicated single-workspace deployment. |
| `HYREFAST_ALLOW_WORKSPACE_ID_PARAM` | No | `false` | Allows `secure_read_cypher` to obtain `workspace_id` from tool parameters. Use only for the POC multi-workspace demo bridge; the caller can select a workspace. |
| `HYREFAST_ADMIN_READ_TOKEN` | Required for admin tools to succeed | Empty | Server-to-server secret checked by `admin_get_schema` and `admin_read_cypher`. Configure the same secret in the Agent service. |

`admin_get_schema` and `admin_read_cypher` are registered even if
`HYREFAST_ADMIN_READ_TOKEN` is empty, so they are discoverable after
deployment. Calls fail closed until the token is configured.

## Client-Only Environment Variable

`mcp_client.py` is a simple tool-discovery check. It does not configure the
server.

| Variable | Used by | Example | Need / utility |
| --- | --- | --- | --- |
| `MCP_SERVER_URL` | `mcp_client.py` only | `https://your-service.run.app/mcp` | URL of an already running adapter to inspect discoverable tools. |

## Do Not Put These In The Adapter

These belong to the Agent service, not to this adapter:

| Variable | Why it belongs elsewhere |
| --- | --- |
| `HYREFAST_REVIEW_ADMIN_TOKEN` | Authenticates a human/browser to the Agent chat and review endpoints. |
| `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | Used by the Agent's language model, not Neo4j MCP query execution. |
| `NEO4J_ADAPTER_MCP_URL` | Tells the Agent where this adapter lives. |

## Configuration Recipes

### 1. Secure Workspace Deployment For One Tenant

Use when one adapter instance is dedicated to one authenticated workspace.
This is the strongest tenant boundary currently available because workspace
scope is configured server-side.

```env
NEO4J_URI=bolt://neo4j-host:7687
NEO4J_USERNAME=hyrefast_reader
NEO4J_PASSWORD=<neo4j-read-password>
NEO4J_DATABASE=neo4j

NEO4J_NAMESPACE=
NEO4J_TRANSPORT=http
NEO4J_MCP_SERVER_HOST=0.0.0.0
NEO4J_MCP_SERVER_PORT=8080
NEO4J_MCP_SERVER_PATH=/mcp
NEO4J_MCP_SERVER_ALLOWED_HOSTS=<adapter-hostname>
NEO4J_MCP_SERVER_STATELESS=true

NEO4J_READ_ONLY=true
NEO4J_EXPOSE_RAW_TOOLS=false
NEO4J_SCHEMA_SAMPLE_SIZE=1000
NEO4J_READ_TIMEOUT=30
NEO4J_RESPONSE_TOKEN_LIMIT=

HYREFAST_WORKSPACE_ID=<trusted-workspace-id>
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=false
HYREFAST_ADMIN_READ_TOKEN=<server-to-server-admin-secret>
```

Use this mode when workspace isolation matters more than supporting many
workspace identities through one demo UI.

### 2. POC Demo With Switchable Workspace IDs And Admin Chat

Use when the current chat modal must let a tester enter different workspace
IDs and switch to admin. This is not production tenant authentication because
the workspace ID is caller-provided.

Adapter service:

```env
NEO4J_URI=bolt://neo4j-host:7687
NEO4J_USERNAME=hyrefast_reader
NEO4J_PASSWORD=<neo4j-read-password>
NEO4J_DATABASE=neo4j

NEO4J_TRANSPORT=http
NEO4J_MCP_SERVER_HOST=0.0.0.0
NEO4J_MCP_SERVER_PORT=8080
NEO4J_MCP_SERVER_PATH=/mcp
NEO4J_MCP_SERVER_ALLOWED_HOSTS=<adapter-hostname>
NEO4J_MCP_SERVER_STATELESS=true

NEO4J_READ_ONLY=true
NEO4J_EXPOSE_RAW_TOOLS=false
NEO4J_READ_TIMEOUT=30

HYREFAST_WORKSPACE_ID=
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=true
HYREFAST_ADMIN_READ_TOKEN=<server-to-server-admin-secret>
```

Agent service must contain the same adapter secret plus its separate human
admin login secret:

```env
NEO4J_ADAPTER_MCP_URL=https://<adapter-hostname>/mcp
HYREFAST_ADMIN_READ_TOKEN=<server-to-server-admin-secret>
HYREFAST_REVIEW_ADMIN_TOKEN=<human-admin-login-secret>
```

Use different values for `HYREFAST_ADMIN_READ_TOKEN` and
`HYREFAST_REVIEW_ADMIN_TOKEN`.

### 3. Read-Only Local Python Testing

Create `.env` in this folder with local Neo4j details:

```env
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<local-password>
NEO4J_DATABASE=neo4j
NEO4J_TRANSPORT=http
NEO4J_MCP_SERVER_HOST=127.0.0.1
NEO4J_MCP_SERVER_PORT=8080
NEO4J_MCP_SERVER_PATH=/mcp
NEO4J_MCP_SERVER_ALLOWED_HOSTS=127.0.0.1:8080,localhost:8080,127.0.0.1,localhost
NEO4J_MCP_SERVER_STATELESS=true
NEO4J_READ_ONLY=true
NEO4J_EXPOSE_RAW_TOOLS=false
HYREFAST_WORKSPACE_ID=
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=true
HYREFAST_ADMIN_READ_TOKEN=<local-admin-tool-secret>
MCP_SERVER_URL=http://127.0.0.1:8080/mcp
```

Run:

```powershell
pip install -r requirements.txt
python server.py
```

In a second terminal:

```powershell
python mcp_client.py
```

### 4. Docker Compose Local Testing

The provided `docker-compose.yml` loads `.env` and publishes port `8080`.
Use the same local `.env` recipe above, with:

```env
NEO4J_MCP_SERVER_HOST=0.0.0.0
NEO4J_MCP_SERVER_PORT=8080
```

Run:

```powershell
docker compose up --build
```

### 5. Raw Tool Debugging Only

Use only against a trusted local environment while diagnosing Neo4j queries or
schema. Never deploy this configuration for the Agent.

```env
NEO4J_EXPOSE_RAW_TOOLS=true
NEO4J_READ_ONLY=true
```

This makes raw schema and raw read tools discoverable but continues to omit raw
writes. To expose raw writes for a disposable development database only:

```env
NEO4J_EXPOSE_RAW_TOOLS=true
NEO4J_READ_ONLY=false
```

## Cloud Run Deployment Steps

1. Confirm the adapter code includes `secure_get_schema`,
   `secure_read_cypher`, `admin_get_schema`, and `admin_read_cypher`.
2. Create or select a Google Cloud project and enable Cloud Run build/deploy
   support.
3. Build and deploy this directory as a Cloud Run service using the supplied
   `Dockerfile`.
4. Configure the service environment variables using the relevant recipe
   above. Store `NEO4J_PASSWORD` and `HYREFAST_ADMIN_READ_TOKEN` as secrets
   rather than committing them in `.env`.
5. Set `NEO4J_MCP_SERVER_ALLOWED_HOSTS` to the actual Cloud Run service
   hostname.
6. Keep `NEO4J_READ_ONLY=true` and `NEO4J_EXPOSE_RAW_TOOLS=false` for the
   Agent-facing deployment.
7. Deploy or redeploy the service.
8. From a trusted terminal, verify tool discovery:

```powershell
$env:MCP_SERVER_URL="https://<cloud-run-service-host>/mcp"
python -B mcp_client.py
```

9. Confirm these tools are listed:

```text
secure_get_schema
secure_read_cypher
admin_get_schema
admin_read_cypher
```

10. Configure the Agent with `NEO4J_ADAPTER_MCP_URL` and the same
    `HYREFAST_ADMIN_READ_TOKEN`, then restart the Agent so it reloads tool
    discovery.

## Expected Security Behavior

| Request | Workspace role | Admin role |
| --- | --- | --- |
| Global taxonomy read | Allowed via `secure_read_cypher` | Allowed |
| Candidates in one authenticated workspace | Allowed via `secure_read_cypher` | Allowed when deliberately scoped |
| List all workspaces | Rejected by `secure_read_cypher` | Allowed via `admin_read_cypher` |
| Count candidates per workspace | Rejected by `secure_read_cypher` | Allowed via `admin_read_cypher` |
| Full private/admin schema inspection | Not provided by `secure_get_schema` | Allowed via `admin_get_schema` |
| Writes via adapter in recommended deployment | Not exposed | Not exposed |

## Deployment Checklist

- [ ] Neo4j credentials are provided through deployment secrets.
- [ ] `NEO4J_READ_ONLY=true`.
- [ ] `NEO4J_EXPOSE_RAW_TOOLS=false`.
- [ ] Host allowlist matches the deployed hostname.
- [ ] Workspace mode is intentionally selected: fixed trusted workspace or POC parameter mode.
- [ ] `HYREFAST_ADMIN_READ_TOKEN` is configured in both adapter and Agent.
- [ ] The human Agent admin token is different from the adapter token.
- [ ] Adapter tools have been rediscovered after deployment by restarting the Agent.

## Production Security Gate

The checklist above supports the current POC deployment. A production
deployment must additionally follow the Agent security and modular-design
standard in
[PRODUCTION_SECURITY_AND_LLD.md](../Agent/PRODUCTION_SECURITY_AND_LLD.md).

At minimum, production must:

- Derive user role, workspace scope, and permissions from validated identity
  and server-side authorization data.
- Remove browser-entered workspace scope and shared human admin tokens.
- Set `HYREFAST_ALLOW_WORKSPACE_ID_PARAM=false`; workspace scope must be
  injected by a trusted backend wrapper.
- Keep this adapter private to authenticated services or validate signed
  service identity before tool execution.
- Use read-only Neo4j privileges for read tools, managed secrets, durable
  redacted auditing, and automated tenant-isolation/authorization tests.
- Reject startup or deployment when demo bypasses, raw tools, or write
  capability are enabled in the production read service.
