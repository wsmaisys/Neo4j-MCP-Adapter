# Neo4j MCP Adapter Deployment Guide

This folder contains the deployed Neo4j MCP adapter that serves Streamable HTTP at `/mcp`.

## Files

- `server.py` runs the MCP adapter.
- `.env` holds the server-side Neo4j connection and adapter runtime settings.
- `docker-compose.yml` starts the adapter container.
- `client_config.example.json` is only for the MCP client side.

## How it fits together

- The client talks only to the MCP adapter URL.
- The adapter talks to Neo4j over Bolt.
- Neo4j credentials never go in the client config.

## Required server-side variables

- `NEO4J_URI`
- `NEO4J_USERNAME`
- `NEO4J_PASSWORD`
- `NEO4J_DATABASE`
- `NEO4J_MCP_SERVER_HOST`
- `NEO4J_MCP_SERVER_PORT`
- `NEO4J_MCP_SERVER_PATH`

## Run with Docker Compose

```bash
docker compose up --build -d
```

The compose file reads `.env` automatically.

## Client config example

Use the public MCP URL in the client's MCP config:

```json
{
  "mcpServers": {
    "neo4j": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://neo4j-mcp-adapter-736344442420.us-central1.run.app"]
    }
  }
}
```

## Notes

- Keep `.env` out of source control.
- Put the adapter behind HTTPS for production.
- The adapter is already configured to fail fast if required environment variables are missing.
