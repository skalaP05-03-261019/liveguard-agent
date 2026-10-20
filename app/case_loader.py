"""Read-only case loading. Business data remains behind the MCP boundary."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, ValidationError, model_validator

from app.clients.business_mcp import AgentToolError
from app.clients.ports import ReadTools
from app.schemas import Contract, TaskEnvelope
from app.state import AgentState


class CaseSnapshot(Contract):
    # BE may add fields; only these fields are consumed by this implementation proposal.
    model_config = ConfigDict(alias_generator=Contract.model_config["alias_generator"],
                              populate_by_name=True, extra="ignore")
    case_id: UUID
    snapshot_id: UUID
    evidence_version: int = Field(ge=1)
    policy_version: str = Field(min_length=1)
    target_message_ids: list[UUID] = Field(min_length=1)
    target_versions: dict[UUID, Annotated[int, Field(ge=1)]]
    # New team contract fields; older read-only callers remain compatible.
    # The integrated workflow requires these explicitly and never substitutes channelId.
    live_id: UUID | None = None
    user_id: UUID | None = None
    handling_route: Literal["PASS", "RULE_CONFIRMED", "CONTEXT_REVIEW",
                            "PII_INVESTIGATION", "FLOOD_REVIEW"] | None = None
    kick_review: dict | None = None
    confirmation_status: Literal["CONFIRMED", "UNCONFIRMED"] | None = None

    @model_validator(mode="after")
    def validate_targets(self):
        if len(self.target_message_ids) != len(set(self.target_message_ids)):
            raise ValueError("targetMessageIds must be unique")
        if not set(self.target_message_ids) <= self.target_versions.keys():
            raise ValueError("Every target requires a target version")
        return self


class ChatMessage(Contract):
    model_config = CaseSnapshot.model_config
    message_id: UUID
    author_alias: str
    text: str
    sent_at: AwareDatetime
    visibility: Annotated[str, Field(pattern="^(PUBLIC|HELD|HIDDEN)$")]
    reply_to_id: UUID | None = None


class ChatContext(Contract):
    model_config = CaseSnapshot.model_config
    case_id: UUID
    snapshot_id: UUID
    evidence_version: int = Field(ge=1)
    messages: list[ChatMessage]


async def load_case_data(task: TaskEnvelope, tools: ReadTools) -> AgentState:
    trace_id = str(task.trace_id)
    args = {"caseId": str(task.case_id), "traceId": trace_id}
    case_response = await tools.call("get_moderation_case", args)
    try:
        snapshot = CaseSnapshot.model_validate(case_response["data"])
    except (KeyError, TypeError, ValidationError):
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id) from None
    if snapshot.case_id != task.case_id:
        raise AgentToolError("INVALID_EVIDENCE", trace_id=trace_id)
    if (snapshot.snapshot_id != task.snapshot_id
            or snapshot.evidence_version != task.expected_evidence_version):
        raise AgentToolError("STALE_EVIDENCE", trace_id=trace_id)

    context_args = {**args, "before": 5, "after": 5}
    context_response = await tools.call("get_chat_context", context_args)
    try:
        context = ChatContext.model_validate(context_response["data"])
    except (KeyError, TypeError, ValidationError):
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id) from None
    if context.case_id != task.case_id:
        raise AgentToolError("INVALID_EVIDENCE", trace_id=trace_id)
    if (context.snapshot_id != task.snapshot_id
            or context.evidence_version != task.expected_evidence_version):
        raise AgentToolError("STALE_EVIDENCE", trace_id=trace_id)
    ids = [message.message_id for message in context.messages]
    if len(ids) != len(set(ids)) or not set(snapshot.target_message_ids) <= set(ids):
        raise AgentToolError("INVALID_EVIDENCE", trace_id=trace_id)

    messages = [message.model_dump(mode="json", by_alias=True) for message in context.messages]
    targets = {str(message_id) for message_id in snapshot.target_message_ids}
    patch = {
        "task": task,
        "case_id": str(task.case_id), "task_id": str(task.task_id),
        "thread_id": task.thread_id, "snapshot_id": str(task.snapshot_id),
        "trace_id": trace_id, "evidence_version": snapshot.evidence_version,
        "policy_version": snapshot.policy_version,
        "case_snapshot": snapshot.model_dump(mode="json", by_alias=True),
        "messages": messages,
        "target_messages": [m for m in messages if m["messageId"] in targets],
        "context_messages": [m for m in messages if m["messageId"] not in targets],
        # Minimal observations: chat text and credentials remain out of generic traces.
        "tool_observations": [
            {"toolName": "get_moderation_case", "traceId": trace_id},
            {"toolName": "get_chat_context", "traceId": trace_id},
        ],
    }
    for key, value in (("live_id", snapshot.live_id), ("user_id", snapshot.user_id),
                       ("handling_route", snapshot.handling_route)):
        if value is not None:
            patch[key] = str(value)
    if snapshot.kick_review is not None:
        patch["kick_review"] = snapshot.kick_review
    return patch
