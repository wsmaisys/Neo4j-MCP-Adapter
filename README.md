# Neo4j MCP Adapter

This project connects an MCP client to a Neo4j database.

The code stays the same for everyone.
Only the `.env` file changes for each person or deployment.

## What this does

- Connects to your Neo4j database
- Lets an MCP client read Neo4j data over HTTP
- Exposes secure HyreFast workspace-aware read tools by default
- Can run locally or on Google Cloud Run
- Can be locked to read-only mode so no write tool is exposed

## What you need

- A Neo4j database
- Python 3.12 or later
- Docker if you want to run it locally in a container
- A Google Cloud project if you want to deploy it on Cloud Run

## What to change for your own setup

For most people, only these values in `.env` need to change:

- `NEO4J_URI`
- `NEO4J_USERNAME`
- `NEO4J_PASSWORD`
- `NEO4J_DATABASE`
- `NEO4J_READ_ONLY`

If your deployment is different, you may also change:

- `NEO4J_MCP_SERVER_HOST`
- `NEO4J_MCP_SERVER_PORT`
- `NEO4J_MCP_SERVER_PATH`
- `NEO4J_MCP_SERVER_ALLOWED_HOSTS`

## Step 1: Set up `.env`

Create a `.env` file in the project root.

Put your own Neo4j details there.

Example:

```env
NEO4J_URI=bolt://your-neo4j-host:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your-password
NEO4J_DATABASE=neo4j

NEO4J_NAMESPACE=
NEO4J_TRANSPORT=http

NEO4J_MCP_SERVER_HOST=0.0.0.0
NEO4J_MCP_SERVER_PORT=8080
NEO4J_MCP_SERVER_PATH=/mcp
NEO4J_MCP_SERVER_ALLOW_ORIGINS=
NEO4J_MCP_SERVER_ALLOWED_HOSTS=*
NEO4J_MCP_SERVER_STATELESS=true

NEO4J_READ_ONLY=true
NEO4J_EXPOSE_RAW_TOOLS=false
NEO4J_SCHEMA_SAMPLE_SIZE=1000
NEO4J_READ_TIMEOUT=30
NEO4J_RESPONSE_TOKEN_LIMIT=

HYREFAST_WORKSPACE_ID=your-workspace-id
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=false
```

If you want write access, change:

```env
NEO4J_READ_ONLY=false
```

## Step 2: Run locally

### Option A: Run with Python

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Start the server:

```bash
python server.py
```

### Option B: Run with Docker

1. Make sure Docker is running.
2. Start the server:

```bash
docker compose up --build
```

The compose file reads `.env` automatically.

## Step 3: Connect an MCP client

Point your MCP client to the server URL.

Example:

```json
{
  "mcpServers": {
    "neo4j-adapter": {
      "url": "https://your-domain-or-cloud-run-url/mcp",
      "transport": "streamable_http"
    }
  }
}
```

If you use the Python demo client in this repo, update the URL inside `mcp_client.py` to your own endpoint.

## How to deploy for private use

This is the simple workflow:

1. Keep the code unchanged.
2. Change only `.env` for your own Neo4j server and deployment settings.
3. Build and deploy the container to your own Google Cloud Run service.
4. Set `NEO4J_READ_ONLY=true` if you want the adapter to expose only read tools.
5. Give your MCP client the Cloud Run URL ending in `/mcp`.

### Google Cloud Run steps

1. Log in to Google Cloud.
2. Select your project.
3. Deploy this repository as a Cloud Run service.
4. Set the runtime environment variables from `.env` in the Cloud Run service settings.
5. Make sure the service listens on port `8080`.
6. Redeploy the service after changes.

If you only change `.env` values in Cloud Run and redeploy, the code does not need to change.

Important: use `/mcp` as the path. Do not add a trailing slash in client URLs or config files.

### Required Cloud Run environment variables for secure mode

When deploying this adapter for HyreFast workspace-user chat, make sure Cloud Run has these values:

```env
NEO4J_READ_ONLY=true
NEO4J_EXPOSE_RAW_TOOLS=false
```

For temporary POC testing with a UI popup that asks for `workspace_id`, use:

```env
HYREFAST_WORKSPACE_ID=
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=true
```

In this mode, the caller may pass `workspace_id` in the `secure_read_cypher` params object. This is only for POC testing because the caller controls the workspace id.

For a fixed server-side workspace, use:

```env
HYREFAST_WORKSPACE_ID=your-workspace-id
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=false
```

For the deployed Cloud Run service, keep the allowed host set to the Cloud Run service hostname:

```env
NEO4J_MCP_SERVER_ALLOWED_HOSTS=neo4j-mcp-adapter-736344442420.us-central1.run.app
```

If testing locally, override the host allowlist in the terminal instead:

```powershell
$env:NEO4J_MCP_SERVER_ALLOWED_HOSTS="127.0.0.1:8080,localhost:8080,127.0.0.1,localhost"
```

### Post-deploy verification

After pushing changes and waiting for CI/CD to redeploy Cloud Run, verify the deployed adapter:

```powershell
cd "C:\Yegawasim\GitHub\Neo4j-MCP-Adapter"
$env:MCP_SERVER_URL="https://neo4j-mcp-adapter-736344442420.us-central1.run.app/mcp"
python -B mcp_client.py
```

Expected tools:

```text
secure_get_schema
secure_read_cypher
```

If you see raw tools like `read_neo4j_cypher`, `get_neo4j_schema`, or `write_neo4j_cypher`, check that:

```env
NEO4J_EXPOSE_RAW_TOOLS=false
```

## Read-only mode

If you want this adapter to be safe for general use, set:

```env
NEO4J_READ_ONLY=true
```

In read-only mode, the write tool is not registered at all.

## Secure HyreFast tools

By default, the adapter exposes these workspace-safe tools:

```text
secure_get_schema
secure_read_cypher
```

`secure_read_cypher` allows flexible Text2Cypher, but enforces these rules:

- Write operations are blocked.
- Candidate and candidate-side labels must be scoped through `Workspace`.
- The required candidate pattern is:

```cypher
(:Workspace {workspace_id: $workspace_id})-[:LISTED_CANDIDATE]->(:Candidate)
```

- The adapter injects `workspace_id`.
- Global taxonomy reads over `Skill`, `Category`, `Subcategory`, `Alias`, `Tag`, `JobRole`, and `Taxonomy` can run without workspace scope.

For normal deployment, set the workspace on the server:

```env
HYREFAST_WORKSPACE_ID=your-workspace-id
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=false
```

For temporary POC testing only, you may let the caller pass `workspace_id` in tool params:

```env
HYREFAST_WORKSPACE_ID=
HYREFAST_ALLOW_WORKSPACE_ID_PARAM=true
```

This is useful if the POC UI shows a startup popup asking for a workspace id before chat begins. Do not treat this as production tenant security, because the caller can choose the workspace id.

## Raw Neo4j tools

Raw tools are disabled by default:

```text
get_neo4j_schema
read_neo4j_cypher
write_neo4j_cypher
```

If you need them for local debugging, explicitly set:

```env
NEO4J_EXPOSE_RAW_TOOLS=true
```

Keep this false for workspace-user deployments.

## Files in this repo

- `server.py` is the MCP server
- `mcp_client.py` is a small test client
- `docker-compose.yml` runs the server with Docker
- `.env` holds the deployment settings

## Simple rule to remember

If two people use this project, the code stays the same.
Only the `.env` file changes.
