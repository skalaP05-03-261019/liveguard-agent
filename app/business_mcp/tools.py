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
    ReleaseHeldCommand,
    TimeoutCommand,
)


class BackendOperations(Protocol):
    async def get_case_context(
        self,
        case_id: UUID,
        *,
        view: str,
        trace_id: str,
        params: dict[str, int] | None = None,
    ) -> dict[str, Any]: ...

    async def get_user_history(
        self, case_id: UUID, *, days: int, trace_id: str
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
    if trace_id is None:
        return str(uuid4())
    return str(trace_id)


def _success(data: dict[str, Any], trace_id: str) -> dict[str, Any]:
    return {"data": data, "traceId": trace_id}


def _raise_tool_error(error: BackendError) -> None:
    raise ToolError(error.as_tool_error()) from error


def register_tools(
    mcp: MCPServer,
    get_backend: Callable[[], BackendOperations],
) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def get_moderation_case(
        caseId: UUID, traceId: UUID | None = None
    ) -> dict[str, Any]:
        """Get the moderation snapshot, evidence version, policy version, and targets."""
        trace_id = _trace_id(traceId)
        try:
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
        before: Annotated[int, Field(ge=0, le=50)] = 5,
        after: Annotated[int, Field(ge=0, le=50)] = 5,
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
        days: Annotated[int, Field(ge=1)] = 30,
        traceId: UUID | None = None,
    ) -> dict[str, Any]:
        """Get confirmed moderation history for the user involved in a case."""
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
    if command.type != expected_type:
        raise ToolError(f"actionCommand.type must be {expected_type}")
    resolved_trace_id = _trace_id(trace_id)
    try:
        data = await backend.execute_action(command, trace_id=resolved_trace_id)
    except BackendError as error:
        _raise_tool_error(error)
    return _success(data, resolved_trace_id)
