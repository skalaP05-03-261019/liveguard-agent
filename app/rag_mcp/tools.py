import json
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.types import ToolAnnotations

from app.rag_mcp.embeddings import QueryEmbedder
from app.rag_mcp.policy import DOCUMENT_ID
from app.rag_mcp.repository import PolicyRepository
from app.schemas import Category

PolicyId = Annotated[str, Field(pattern=r"^(CP|SP|MP|MM)-\d+\.\d+$")]
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


class RagError(Exception):
    """Business MCP와 같은 오류 envelope로 Agent에 전달한다."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_tool_error(self, trace_id: str) -> str:
        return json.dumps(
            {
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "retryable": self.retryable,
                },
                "traceId": trace_id,
            },
            ensure_ascii=False,
        )


def _trace_id(trace_id: UUID | None) -> str:
    # 작업 trace를 전달받으면 유지하고, 단독 호출에서만 새 ID를 만든다.
    return str(uuid4()) if trace_id is None else str(trace_id)


def _section(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "policyId": row["policy_id"],
        "title": row["title"],
        "text": row["text"],
        "version": row["version"],
        "category": row["category"],
        "resourceUri": row["resource_uri"],
    }


def render_resource(row: dict[str, Any]) -> str:
    # 근거 패널용 원문. 본문은 도구 결과의 text와 같은 값이다.
    return f"## {row['policy_id']} {row['title']}\n\n{row['text']}\n"


async def _require_version(
    repository: PolicyRepository, version: str, embedding_model: str | None
) -> None:
    versions = await repository.list_versions()
    if version not in versions:
        available = ", ".join(versions) or "없음"
        raise RagError(
            "POLICY_VERSION_NOT_FOUND",
            f"policyVersion '{version}'은 색인되지 않았다. 사용 가능한 버전: {available}",
            retryable=False,
        )
    # 질의와 색인의 임베딩 모델이 다르면 점수가 의미 없으므로 검색하지 않는다.
    if embedding_model is not None and versions[version] != embedding_model:
        raise RagError(
            "POLICY_INDEX_MISMATCH",
            f"policyVersion '{version}' 색인 모델({versions[version]})과 "
            f"질의 모델({embedding_model})이 다르다. 재색인이 필요하다.",
            retryable=False,
        )


def register_tools(
    mcp: MCPServer,
    get_repository: Callable[[], PolicyRepository],
    get_embedder: Callable[[], QueryEmbedder],
) -> None:
    @mcp.tool(annotations=_READ_ONLY)
    async def search_policy(
        query: Annotated[str, Field(min_length=1, max_length=1000)],
        policyVersion: Annotated[str, Field(min_length=1)],
        category: Category | None = None,
        topK: Annotated[int, Field(ge=1, le=4)] = 4,
        traceId: UUID | None = None,
    ) -> dict[str, Any]:
        """운영정책에서 위반 정의·예외·제재 조건 조항을 의미 검색한다.

        query는 판단에 필요한 내용을 자연어로 적는다(예: "욕설 키워드가 포함된 채팅의 처리 기준").
        category를 주면 해당 위험 유형 조항과 모든 유형에 적용되는 공통 조항만 검색한다.
        결과는 유사도 순 조항 목록이며 policyId는 판단 근거(policyIds)로, resourceUri는 원문 열람에 쓴다.
        """
        trace_id = _trace_id(traceId)
        repository = get_repository()
        embedder = get_embedder()
        try:
            await _require_version(repository, policyVersion, embedder.model)
            vector = await embedder.embed_query(query)
            rows = await repository.search(
                vector, version=policyVersion, category=category, top_k=topK
            )
        except RagError as error:
            raise ToolError(error.as_tool_error(trace_id)) from error
        except Exception as error:
            # DB·임베딩 API의 일시 장애. 재시도 여부는 Agent Harness 예산으로 판단한다.
            unavailable = RagError(
                "TEMPORARY_UNAVAILABLE", "정책 검색을 일시적으로 수행할 수 없다.", retryable=True
            )
            raise ToolError(unavailable.as_tool_error(trace_id)) from error
        matches = [
            {**_section(row), "score": round(float(row["score"]), 4)} for row in rows
        ]
        return {
            "data": {"policyVersion": policyVersion, "matches": matches},
            "traceId": trace_id,
        }

    @mcp.tool(annotations=_READ_ONLY)
    async def get_policy_section(
        policyId: PolicyId,
        policyVersion: Annotated[str, Field(min_length=1)],
        traceId: UUID | None = None,
    ) -> dict[str, Any]:
        """조항 ID로 운영정책 조항 원문 하나를 정확히 조회한다(예: policyId "MP-4.1").

        search_policy 결과나 판단 근거에 인용한 조항을 다시 확인할 때 쓴다.
        """
        trace_id = _trace_id(traceId)
        repository = get_repository()
        try:
            await _require_version(repository, policyVersion, None)
            row = await repository.get_section(policyId, policyVersion)
            if row is None:
                ids = ", ".join(await repository.list_policy_ids(policyVersion))
                raise RagError(
                    "POLICY_SECTION_NOT_FOUND",
                    f"'{policyId}' 조항이 v{policyVersion}에 없다. 있는 조항: {ids}",
                    retryable=False,
                )
        except RagError as error:
            raise ToolError(error.as_tool_error(trace_id)) from error
        except Exception as error:
            unavailable = RagError(
                "TEMPORARY_UNAVAILABLE", "정책 조항을 일시적으로 조회할 수 없다.", retryable=True
            )
            raise ToolError(unavailable.as_tool_error(trace_id)) from error
        return {"data": _section(row), "traceId": trace_id}

    @mcp.resource(
        "policy://{version}/{document_id}/{section_id}",
        name="policy_section",
        description="운영정책 조항 원문 (근거 패널용). search_policy 결과의 resourceUri로 읽는다.",
        mime_type="text/markdown",
    )
    async def policy_section(version: str, document_id: str, section_id: str) -> str:
        if document_id != DOCUMENT_ID:
            raise ResourceError(f"unknown policy document '{document_id}'")
        row = await get_repository().get_section(section_id, version)
        if row is None:
            raise ResourceError(f"policy section '{section_id}' not found in v{version}")
        return render_resource(row)
