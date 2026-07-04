"""
NeoLook MCP Server.

Creates one shared ShopifyClient and registers every tool tier against it.
Normal usage (Claude Desktop / Claude Code) launches this over stdio, which
is 1:1 with the client process - a new client means a fresh server, and a
cold in-memory cache. Setting NEOLOOK_TRANSPORT=streamable-http instead
runs this as a standalone long-lived HTTP server (host/port via
NEOLOOK_HTTP_HOST/NEOLOOK_HTTP_PORT) that many separate client processes
can share - used by the eval harness so a whole pass's worth of tasks
benefits from one warm cache, the way a real long-running agent session
would (see docs/BUILD_LOG.md Phase 7).
"""

import os
from typing import Literal, cast

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from neolook.shopify_client import ShopifyClient
from neolook.tools import agentic, analytics, crud

load_dotenv()

_transport = cast(
    'Literal["stdio", "sse", "streamable-http"]', os.environ.get("NEOLOOK_TRANSPORT", "stdio")
)

mcp = FastMCP(
    "neolook-mcp-server",
    host=os.environ.get("NEOLOOK_HTTP_HOST", "127.0.0.1"),
    port=int(os.environ.get("NEOLOOK_HTTP_PORT", "8000")),
)
# NEOLOOK_METRICS_FILE lets metrics accumulate across separate short-lived
# processes (e.g. the eval harness launches a fresh server subprocess per
# task) instead of resetting to zero each time. Unset for normal use.
_client = ShopifyClient(metrics_file=os.environ.get("NEOLOOK_METRICS_FILE"))

crud.register(mcp, _client)
analytics.register(mcp, _client)
agentic.register(mcp, _client)


def main() -> None:
    mcp.run(transport=_transport)


if __name__ == "__main__":
    main()
