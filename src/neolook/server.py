"""
NeoLook MCP Server.

Creates one shared ShopifyClient and registers every tool tier against it.
Normal usage (Claude Desktop / Claude Code) launches this over stdio, which
is 1:1 with the client process - a new client means a fresh server, and a
cold in-memory cache. Setting NEOLOOK_TRANSPORT=streamable-http instead
runs this as a standalone long-lived HTTP server (host/port via
NEOLOOK_HTTP_HOST/NEOLOOK_HTTP_PORT, or the cloud-standard PORT env var
if set) that many separate client processes can share - used by the eval
harness so a whole pass's worth of tasks benefits from one warm cache
(see docs/BUILD_LOG.md Phase 8), and by a public deployment for use as a
claude.ai custom connector (see docs/BUILD_LOG.md Phase 10).

claude.ai's custom-connector UI only supports OAuth for authenticating a
remote MCP server - implementing a real OAuth authorization server is
substantial extra scope for a demo project connected to a sandbox dev
store. Instead, when NEOLOOK_CONNECTOR_SECRET is set, every request must
carry a matching `key` query parameter (embedded directly in the
connector URL, e.g. `https://host/mcp?key=...`) or gets a 401 - a
deliberately simple, documented tradeoff, not a claim of real OAuth-grade
security. Unset for local/eval use, where the network boundary itself
(localhost, or a private tunnel) is the actual protection.

For the HTTP transport, FastMCP's own streamable-HTTP Starlette app is
mounted inside a FastAPI app (rather than run standalone), and the
shared-secret check is ordinary FastAPI middleware - the pattern FastMCP
itself documents for "mounting multiple FastMCP servers in a single
FastAPI application" (see FastMCP.session_manager's docstring).
"""

import contextlib
import hmac
import os
from typing import AsyncIterator, Literal, cast

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
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


def _build_fastapi_app() -> FastAPI:
    """Mounts FastMCP's streamable-HTTP app inside a FastAPI app."""
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # FastMCP's session manager owns the streamable-HTTP transport's
        # request/response plumbing and must be running for the mounted
        # app to work at all - mounting alone doesn't start it, since a
        # sub-app's own lifespan isn't invoked automatically by its parent.
        async with mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/", mcp_app)

    secret = os.environ.get("NEOLOOK_CONNECTOR_SECRET")
    if secret:
        @app.middleware("http")
        async def _require_shared_secret(request: Request, call_next):
            provided = request.query_params.get("key", "")
            if not hmac.compare_digest(provided, secret):
                return PlainTextResponse("Unauthorized", status_code=401)
            return await call_next(request)

    return app


def main() -> None:
    if _transport != "streamable-http":
        mcp.run(transport=_transport)
        return

    app = _build_fastapi_app()

    # Cloud platforms (Render, Heroku, etc.) assign a port via $PORT and
    # expect the app to bind 0.0.0.0; NEOLOOK_HTTP_PORT/HOST remain the
    # local-dev/eval-harness override.
    port = int(os.environ.get("PORT") or os.environ.get("NEOLOOK_HTTP_PORT", "8000"))
    host = os.environ.get("NEOLOOK_HTTP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
