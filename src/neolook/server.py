"""
NeoLook MCP Server - stdio entrypoint.

Creates one shared ShopifyClient and registers every tool tier against it.
This is what Claude Desktop / Claude Code launches to talk to this server.
"""

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from neolook.shopify_client import ShopifyClient
from neolook.tools import analytics, crud

load_dotenv()

mcp = FastMCP("neolook-mcp-server")
_client = ShopifyClient()

crud.register(mcp, _client)
analytics.register(mcp, _client)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
