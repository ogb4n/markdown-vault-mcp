"""Streamable HTTP transport — one shared server for every client.

Stdio hands each client its own process by construction: the transport is a
point-to-point pipe, so five connected clients mean five servers (and, under
Docker, five `docker compose` client processes on the host). Over Streamable
HTTP a single process serves them all, which is the whole point of this module.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .server import app as mcp_server

logger = logging.getLogger("markdown-vault-mcp.http")

DEFAULT_PORT = 27125
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"


class BearerAuth:
    """Guards the endpoint with a shared token.

    Reaching the stdio server means being able to spawn its process, which is
    gate enough. A listening port has no such gate — every process on the
    machine can knock — and these tools write to the vault, so the token is
    what stands between them and it.
    """

    def __init__(self, app: ASGIApp, token: str, public_paths: frozenset[str]) -> None:
        self._app = app
        self._token = token
        self._public_paths = public_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._public_paths:
            await self._app(scope, receive, send)
            return
        if not self._authorized(scope):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers") or ())
        offered = headers.get(b"authorization", b"").decode("latin-1")
        # compare_digest so a wrong token takes the same time as a right one.
        return secrets.compare_digest(offered, f"Bearer {self._token}")


def build_app(
    token: str,
    port: int = DEFAULT_PORT,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
) -> ASGIApp:
    default_hosts = ["127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"]
    default_origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    session_manager = StreamableHTTPSessionManager(
        app=mcp_server,
        # Sessions keep their state in this one process, so there is nothing to
        # replay from and no reason to run stateless.
        stateless=False,
        # The transport refuses unknown Host headers to block DNS rebinding;
        # an empty allow-list would refuse everything, so name the loopback
        # forms a local client can legitimately use. A reverse proxy names its
        # own public host explicitly through MCP_HTTP_ALLOWED_HOSTS.
        security_settings=TransportSecuritySettings(
            allowed_hosts=allowed_hosts or default_hosts,
            allowed_origins=allowed_origins or default_origins,
        ),
    )

    # A class, not a function: Starlette wraps plain functions as
    # request/response endpoints, and only recognises an ASGI app when the
    # endpoint is something else. `Mount` would take an ASGI app directly, but
    # it matches "/mcp" only by redirecting to "/mcp/", and MCP clients post to
    # "/mcp" without following redirects.
    class MCPEndpoint:
        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            await session_manager.handle_request(scope, receive, send)

    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        # The manager owns the per-session tasks; without its context running,
        # every request would be answered by a server that never started.
        async with session_manager.run():
            logger.info("Streamable HTTP transport ready on %s", MCP_PATH)
            yield

    app = Starlette(
        routes=[
            Route(HEALTH_PATH, health),
            # POST carries the requests, GET opens the server-to-client stream,
            # DELETE ends the session.
            Route(MCP_PATH, MCPEndpoint(), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
    return BearerAuth(app, token=token, public_paths=frozenset({HEALTH_PATH}))


def main_http() -> None:
    """Entry point of the `markdown-vault-mcp-http` script."""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

    token = os.getenv("MCP_HTTP_TOKEN")
    if not token:
        raise SystemExit(
            "MCP_HTTP_TOKEN environment variable required: the HTTP transport "
            "exposes vault write access on a local port and will not run open."
        )

    port = int(os.getenv("MCP_HTTP_PORT", str(DEFAULT_PORT)))
    # Binds the loopback by default. In a container it must bind 0.0.0.0 to be
    # reachable at all — the published port is what keeps it on the loopback.
    host = os.getenv("MCP_HTTP_HOST", "127.0.0.1")
    allowed_hosts = _environment_list("MCP_HTTP_ALLOWED_HOSTS")
    allowed_origins = _environment_list("MCP_HTTP_ALLOWED_ORIGINS")

    uvicorn.run(
        build_app(token, port=port, allowed_hosts=allowed_hosts, allowed_origins=allowed_origins),
        host=host,
        port=port,
        log_level="info",
    )


def _environment_list(name: str) -> list[str] | None:
    value = os.getenv(name)
    if not value:
        return None
    values = [item.strip() for item in value.split(",") if item.strip()]
    if not values:
        raise SystemExit(f"{name} must contain at least one value when configured")
    return values
