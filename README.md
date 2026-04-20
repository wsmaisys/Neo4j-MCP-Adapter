# Neo4j MCP Adapter

This repo contains a FastMCP server that exposes Neo4j tools over Streamable HTTP at `/mcp`.

## What Runs In Production

- Cloud Run service: `neo4j-mcp-adapter`
- Public URL: `https://neo4j-mcp-adapter-736344442420.us-central1.run.app`
- MCP endpoint: `https://neo4j-mcp-adapter-736344442420.us-central1.run.app/mcp`
- Transport: `streamable_http`

## Server Configuration

The server reads its runtime settings from environment variables.

Required values:

- `NEO4J_URI`
- `NEO4J_USERNAME`
- `NEO4J_PASSWORD`
- `NEO4J_DATABASE`
- `NEO4J_MCP_SERVER_HOST`
- `NEO4J_MCP_SERVER_PORT`
- `NEO4J_MCP_SERVER_PATH`

Recommended Cloud Run settings for this deployment:

- `NEO4J_MCP_SERVER_HOST=0.0.0.0`
- `NEO4J_MCP_SERVER_PORT=8080`
- `NEO4J_MCP_SERVER_PATH=/mcp/`
- `NEO4J_MCP_SERVER_STATELESS=true`
- `NEO4J_MCP_SERVER_ALLOWED_HOSTS=neo4j-mcp-adapter-736344442420.us-central1.run.app`

Use `NEO4J_MCP_SERVER_ALLOWED_HOSTS=*` only for quick testing.

## Local Run

The simplest local path is:

```bash
python server.py
```

For Docker:

```bash
docker compose up --build -d
```

The compose file reads `.env` automatically.

## Client Configuration

The Python client uses Streamable HTTP and must point at `/mcp` without a trailing slash.

```python
MCP_CONFIG = {
  "neo4j-adapter": {
    "url": "https://neo4j-mcp-adapter-736344442420.us-central1.run.app/mcp",
    "transport": "streamable_http",
  }
}
```

If you use the MCP remote CLI, use the same endpoint:

```json
{
  "mcpServers": {
    "neo4j": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://neo4j-mcp-adapter-736344442420.us-central1.run.app/mcp"
      ]
    }
  }
}
```

## Quick Validation

Run the client:

```bash
python mcp_client.py
```

Probe the MCP handshake:

```bash
curl -i -X POST https://neo4j-mcp-adapter-736344442420.us-central1.run.app/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data-binary @mcp-init.json
```

Where `mcp-init.json` contains a minimal JSON-RPC `initialize` request.

## Free Tier Notes

The current Cloud Run deployment is kept small to stay in the free tier:

- `1 vCPU`
- `512 MiB` memory
- `max instances = 1`
- request-based billing

Cloud Run free-tier limits in `us-central1` are the main guardrails to watch:

- `2 million` requests per month
- `180,000` vCPU-seconds per month
- `360,000` GiB-seconds per month
- `1 GB` outbound data transfer from North America per month

Artifact Registry also has a free storage tier, so keep old images cleaned up.

## Notes

- Keep `.env` out of source control.
- Prefer HTTPS for production deployments.
- The adapter fails fast if the required Neo4j environment variables are missing.
