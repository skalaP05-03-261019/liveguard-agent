from collections.abc import Callable
from typing import Annotated, Any, Protocol
from uuid import UUID, uuid4

from pydantic import Field
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app.business_mcp.schemas import (
    ActionCommand,
    BackendError,
    HideCommand,
    KickCommand,
    ReleaseHeldCommand,
    TimeoutCommand,
)


class BackendOperations(Protocol):
    """도구 로직과 Backend 전송 구현(현재 HTTP adapter)을 분리하는 경계."""

    async def get_case_context(
        self,
        case_id: UUID,
        *,
        view: str,
        trace_id: str,
        params: dict[str, int] | None = None,
    ) -> dict[str, Any]: ...

    async def get_user_history(
        self, case_id: UUID, *, days: int | None, trace_id: str
    ) -> dict[str, Any]: ...

    async def get_action_status(
        self, action_id: UUID, *, trace_id: str
    ) -> dict[str, Any]: ...

    async def execute_action(
        self, command: ActionCommand, *, trace_id: str
    ) -> dict[str, Any]: ...

    async def reconcile_review(
        self, case_id: UUID, decision_id: UUID, *, trace_id: str
    ) -> dict[str, Any]: ...


def _trace_id(trace_id: UUID | None) -> str:
    # 작업 trace를 전달받으면 유지하고, 단독 호출에서만 새 ID를 만든다.
    if trace_id is None:
        return str(uuid4())
    return str(trace_id)


def _success(data: dict[str, Any], trace_id: str) -> dict[str, Any]:
    # Backend의 결과를 공통 계약 envelope에 담아 Agent로 돌려준다.
    return {"data": data, "traceId": trace_id}


def _raise_tool_error(error: BackendError) -> None:
    # 예상 가능한 Backend 오류는 MCP tool 결과로 알려 재시도/검토 판단에 쓰게 한다.
    raise ToolError(error.as_tool_error()) from error


def register_tools(
    mcp: MCPServer,
    get_backend: Callable[[], BackendOperations],
) -> None:
    # 조회/변경 tool이 같은 서버에 등록된다. annotations는 힌트일 뿐 권한 통제가 아니므로,
    # Agent 쪽에서 변경 tool을 모델에 직접 노출하지 않고 코드 경로로만 호출해야 한다.
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_moderation_case(
        caseId: UUID, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Get the moderation snapshot, evidence version, policy version, and targets."""
        trace_id = _trace_id(traceId)
        try:
            # view 값은 내부 API 초안이므로 실제 BE 경로·요청 규격과 맞춰야 한다.
            data = await get_backend().get_case_context(
                caseId,
                view="moderationCase",
                trace_id=trace_id,
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_chat_context(
        caseId: UUID,
        before: Annotated[int, Field(ge=0, le=10)] = 5,
        after: Annotated[int, Field(ge=0, le=10)] = 5,
        traceId: UUID | None = None,
    ) -> dict[str, Any]:
        """Get a minimal, masked window of chat around a moderation case."""
        trace_id = _trace_id(traceId)
        try:
            data = await get_backend().get_case_context(
                caseId,
                view="chatContext",
                trace_id=trace_id,
                params={"before": before, "after": after},
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_user_history(
        caseId: UUID,
        days: Annotated[int, Field(ge=1)] | None = None,
        traceId: UUID | None = None,
    ) -> dict[str, Any]:
        """Get confirmed moderation history; optional days must be positive when provided."""
        trace_id = _trace_id(traceId)
        try:
            data = await get_backend().get_user_history(
                caseId, days=days, trace_id=trace_id
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_stream_info(
        caseId: UUID, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Get broadcast and official event information related to a case."""
        trace_id = _trace_id(traceId)
        try:
            data = await get_backend().get_case_context(
                caseId, view="streamInfo", trace_id=trace_id
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_official_links(
        caseId: UUID, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Check registered official links and domains related to a case."""
        trace_id = _trace_id(traceId)
        try:
            data = await get_backend().get_case_context(
                caseId, view="officialLinks", trace_id=trace_id
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_security_facts(
        caseId: UUID, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Get verified PII and official-status facts, including UNKNOWN when unresolved."""
        trace_id = _trace_id(traceId)
        try:
            data = await get_backend().get_case_context(
                caseId, view="securityFacts", trace_id=trace_id
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_action_status(
        actionId: UUID, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Read the actual status of an existing action; does not execute it."""
        trace_id = _trace_id(traceId)
        try:
            data = await get_backend().get_action_status(
                actionId, trace_id=trace_id
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def hide_message(
        actionCommand: HideCommand, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Execute a Backend-issued HIDE command; never constructs an action plan."""
        return await _execute_expected(
            actionCommand, "HIDE", get_backend(), traceId
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def timeout_user(
        actionCommand: TimeoutCommand, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Execute a Backend-issued TIMEOUT command with its server-calculated duration."""
        return await _execute_expected(
            actionCommand, "TIMEOUT", get_backend(), traceId
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def release_message(
        actionCommand: ReleaseHeldCommand, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Execute a Backend-issued RELEASE_HELD command."""
        return await _execute_expected(
            actionCommand, "RELEASE_HELD", get_backend(), traceId
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def kick_user(
        actionCommand: KickCommand, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Execute an approved, Backend-issued kick command after Backend revalidation."""
        return await _execute_kick(actionCommand, get_backend(), traceId)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    async def reconcile_review(
        caseId: UUID, decisionId: UUID, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Apply a Backend-persisted review decision to cancel, reduce, or restore an action."""
        trace_id = _trace_id(traceId)
        try:
            data = await get_backend().reconcile_review(
                caseId, decisionId, trace_id=trace_id
            )
        except BackendError as error:
            _raise_tool_error(error)
        return _success(data, trace_id)


async def _execute_expected(
    command: ActionCommand,
    expected_type: str,
    backend: BackendOperations,
    trace_id: UUID | None,
) -> dict[str, Any]:
    # tool 이름과 명령 타입이 다르면 Backend에 전달하기 전에 차단한다.
    if command.type != expected_type:
        raise ToolError(f"actionCommand.type must be {expected_type}")
    resolved_trace_id = _trace_id(trace_id)
    try:
        # 현재 상태/버전/권한/멱등성의 최종 검증은 Backend가 다시 수행한다.
        data = await backend.execute_action(command, trace_id=resolved_trace_id)
    except BackendError as error:
        _raise_tool_error(error)
    return _success(data, resolved_trace_id)


async def _execute_kick(
    command: KickCommand,
    backend: BackendOperations,
    trace_id: UUID | None,
) -> dict[str, Any]:
    resolved_trace_id = _trace_id(trace_id)
    max_attempts = 3  # 최초 실행 1회와 재시도 2회

    for attempt in range(1, max_attempts + 1):
        try:
            # 재시도도 같은 actionId/idempotencyKey를 사용해 중복 강퇴를 막는다.
            data = await backend.execute_action(command, trace_id=resolved_trace_id)
        except BackendError as error:
            if not error.retryable:
                _raise_tool_error(error)
            if attempt == max_attempts:
                return _success(
                    {
                        "actionId": str(command.action_id),
                        "status": "FAILED",
                        "attempts": attempt,
                        "errorCode": error.code,
                    },
                    resolved_trace_id,
                )
        else:
            return _success(data, resolved_trace_id)

    raise RuntimeError("Kick retry loop ended unexpectedly")
