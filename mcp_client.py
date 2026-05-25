import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL")

MCP_CONFIG = {
    "neo4j-adapter": {
        "url": MCP_SERVER_URL,
        "transport": "streamable_http",
    }
}

async def main() -> None:
    try:
        client = MultiServerMCPClient(MCP_CONFIG)
        tools = await client.get_tools()

        print("Connected to MCP server(s):")
        for server_name in MCP_CONFIG:
            print(f"- {server_name}: {MCP_CONFIG[server_name]['url']}")

        print("\nAvailable tools:")
        for tool in tools:
            print(f"- {tool.name}")

    except Exception as e:
        print(f"Failed to connect: {e}")

if __name__ == "__main__":
    asyncio.run(main())
