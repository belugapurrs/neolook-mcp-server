"""
NeoLook MCP Server - stdio entrypoint.

Creates one shared ShopifyClient and registers every tool tier against it.
This is what Claude Desktop / Claude Code launches to talk to this server.
"""

import os

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from neolook.shopify_client import ShopifyClient
from neolook.tools import agentic, analytics, crud

load_dotenv()

mcp = FastMCP("neolook-mcp-server")
# NEOLOOK_METRICS_FILE lets metrics accumulate across separate short-lived
# processes (e.g. the eval harness launches a fresh server subprocess per
# task) instead of resetting to zero each time. Unset for normal use.
_client = ShopifyClient(metrics_file=os.environ.get("NEOLOOK_METRICS_FILE"))

crud.register(mcp, _client)
analytics.register(mcp, _client)
agentic.register(mcp, _client)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
