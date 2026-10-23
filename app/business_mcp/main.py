from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hmac

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from app.business_mcp.backend import BackendApi
from app.business_mcp.admission import RequestAdmission
from app.business_mcp.config import Settings
from app.business_mcp.tools import BackendOperations, register_tools
from app.clients.mcp_logging import protect_mcp_logs


def create_app(
    settings: Settings | None = None,
    *,
    backend: BackendOperations | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    mcp = MCPServer("LIVE:GUARD Business MCP")
    protect_mcp_logs()
    app = FastAPI(title="LIVE:GUARD Business MCP", version="0.1.0")
    app.state.started = False
    app.state.draining = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=resolved_settings.backend_base_url,
            timeout=httpx.Timeout(resolved_settings.backend_timeout, connect=3.0, pool=1.0),
            limits=httpx.Limits(max_connections=resolved_settings.backend_connections,
                                max_keepalive_connections=resolved_settings.backend_connections),
            trust_env=False,
        ) as client:
            app.state.backend = backend if backend is not None else BackendApi(resolved_settings, client)
            async with mcp.session_manager.run():
                app.state.draining = False
                app.state.started = True
                try:
                    yield
                finally:
                    app.state.draining = True
                    app.state.started = False
                    app.state.backend = None

    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health() -> dict[str, object]:
        ready = resolved_settings.ready and app.state.started and not app.state.draining
        return {
            "status": "ready" if ready else "not_ready",
            "readyForTasks": ready,
        }

    @app.get("/ready")
    async def readiness():
        status = await health()
        return JSONResponse(status_code=200 if status["readyForTasks"] else 503, content=status)

    @app.middleware("http")
    async def authenticate_agent(request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            configured_token = resolved_settings.agent_token
            if not resolved_settings.ready:
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
            if not hmac.compare_digest(supplied_token.encode(), configured_token.encode()):
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
    app.add_middleware(RequestAdmission, state=app.state,
                       capacity=resolved_settings.max_concurrent_requests)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(resolved_settings.allowed_hosts),
        allowed_origins=[f"http://{host}" for host in resolved_settings.allowed_hosts],
    )
    mcp_http_app: ASGIApp = mcp.streamable_http_app(
        transport_security=security, stateless_http=True, json_response=True,
    )
    app.mount("/", mcp_http_app)
    return app


app = create_app()
