from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hmac

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from starlette.types import ASGIApp

from app.business_mcp.backend import BackendApi
from app.business_mcp.config import Settings
from app.business_mcp.tools import BackendOperations, register_tools


def create_app(
    settings: Settings | None = None,
    *,
    backend: BackendOperations | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    mcp = MCPServer("LIVE:GUARD Business MCP")
    app = FastAPI(title="LIVE:GUARD Business MCP", version="0.1.0")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if backend is not None:
            app.state.backend = backend
            async with mcp.session_manager.run():
                yield
            return

        async with httpx.AsyncClient(
            base_url=resolved_settings.backend_base_url,
            timeout=httpx.Timeout(10.0, connect=3.0),
        ) as client:
            app.state.backend = BackendApi(resolved_settings, client)
            async with mcp.session_manager.run():
                yield

    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ready" if resolved_settings.ready else "not_configured",
            "readyForTasks": resolved_settings.ready,
        }

    @app.middleware("http")
    async def authenticate_agent(request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            configured_token = resolved_settings.agent_token
            if not configured_token:
                return JSONResponse(
                    status_code=503,
                    content={"error": {"code": "UNAUTHENTICATED"}},
                )
            authorization = request.headers.get("authorization", "")
            prefix = "Bearer "
            supplied_token = (
                authorization[len(prefix):]
                if authorization.startswith(prefix)
                else ""
            )
            if not hmac.compare_digest(supplied_token, configured_token):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"code": "UNAUTHENTICATED"}},
                )
        return await call_next(request)

    def get_backend() -> BackendOperations:
        if backend is not None:
            return backend
        current_backend = getattr(app.state, "backend", None)
        if current_backend is None:
            raise RuntimeError("Business MCP backend has not started")
        return current_backend

    register_tools(mcp, get_backend)
    mcp_http_app: ASGIApp = mcp.streamable_http_app()
    app.mount("/", mcp_http_app)
    return app


app = create_app()
