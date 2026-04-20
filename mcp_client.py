import asyncio
from pprint import pprint
from langchain_mcp_adapters.client import MultiServerMCPClient

# Removed the subprocess/gcloud token logic
MCP_CONFIG = {
    "neo4j-adapter": {
        "url": "https://neo4j-mcp-adapter-736344442420.us-central1.run.app/mcp",
        "transport": "streamable_http",
    }
}

async def main() -> None:
    try:
        client = MultiServerMCPClient(MCP_CONFIG)
        tools = await client.get_tools()

        print("Connected to MCP server(s):")
        for server_name in MCP_CONFIG:
            print(f"- {server_name}")

        print("\nAvailable tools:")
        for tool in tools:
            print(f"- {tool.name}")

    except Exception as e:
        print(f"Failed to connect: {e}")

if __name__ == "__main__":
    asyncio.run(main())