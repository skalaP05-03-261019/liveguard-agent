from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp

from app.rag_mcp.config import ServerSettings
from app.rag_mcp.embeddings import AsyncOpenAIEmbedder, QueryEmbedder
from app.rag_mcp.repository import PgPolicyRepository, PolicyRepository
from app.rag_mcp.tools import register_tools


def create_app(
    settings: ServerSettings | None = None,
    *,
    repository: PolicyRepository | None = None,
    embedder: QueryEmbedder | None = None,
) -> FastAPI:
    resolved_settings = settings or ServerSettings.from_env()
    mcp = MCPServer(
        "LIVE:GUARD RAG MCP",
        instructions="운영정책 v3 조항 검색(search_policy), 조항 조회(get_policy_section), "
        "조항 원문 Resource(policy://{version}/{documentId}/{sectionId})를 제공한다.",
    )
    app = FastAPI(title="LIVE:GUARD RAG MCP", version="0.1.0")
    resolved_repository = repository or PgPolicyRepository(resolved_settings.db_url)
    resolved_embedder = embedder or AsyncOpenAIEmbedder(
        resolved_settings.openai_api_key, resolved_settings.embedding_model
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app.router.lifespan_context = lifespan

    @app.get("/health")
    async def health() -> dict[str, object]:
        # 비밀값은 노출하지 않고, 색인된 버전만 보여 준다.
        indexed: list[str] = []
        if resolved_settings.db_url or repository is not None:
            try:
                indexed = list(await resolved_repository.list_versions())
            except Exception:
                indexed = []
        ready = resolved_settings.ready and bool(indexed)
        return {
            "status": "ready" if ready else "not_configured",
            "readyForTasks": ready,
            "indexedPolicyVersions": indexed,
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

    register_tools(mcp, lambda: resolved_repository, lambda: resolved_embedder)
    # 보호는 켜 둔 채 허용 Host만 설정으로 넓힌다(컨테이너 간 호출 시 Host: rag-mcp:8003).
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(resolved_settings.allowed_hosts),
        allowed_origins=[f"http://{host}" for host in resolved_settings.allowed_hosts],
    )
    mcp_http_app: ASGIApp = mcp.streamable_http_app(transport_security=security)
    app.mount("/", mcp_http_app)
    return app


def app() -> FastAPI:
    # uvicorn factory로 기동해 import 시점이 아니라 실행 시점의 환경변수를 읽는다.
    return create_app()
