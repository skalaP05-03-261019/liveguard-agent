"""외부 JSON은 camelCase, Python 필드는 snake_case를 사용한다."""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class TaskEnvelope(Contract):
    event_id: UUID
    task_id: UUID
    case_id: UUID
    kind: Literal["ANALYZE", "REANALYZE", "RESUME_REVIEW", "NOTIFY", "RECONCILE"]
    thread_id: str = Field(min_length=1)
    expected_case_version: int = Field(ge=1)
    decision_id: UUID | None = None
    attempt: int = Field(default=0, ge=0)
    trace_id: UUID

    @model_validator(mode="after")
    def require_review_decision(self):
        if self.kind == "RESUME_REVIEW" and self.decision_id is None:
            raise ValueError("RESUME_REVIEW requires decisionId")
        return self


class Judgment(StrEnum):
    VIOLATION = "VIOLATION"
    NO_VIOLATION = "NO_VIOLATION"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class RequestedAction(Contract):
    # v0.3 proposal only. Execution always uses Backend-issued ActionCommand.
    type: Literal["HIDE", "TIMEOUT", "RELEASE_HELD"]
    message_id: UUID | None = None
    duration_seconds: Literal[300] | None = None

    @model_validator(mode="after")
    def validate_target(self):
        if self.type == "TIMEOUT":
            if self.duration_seconds != 300 or self.message_id is not None:
                raise ValueError("v0.3 TIMEOUT requires 300 seconds and no messageId")
        elif self.message_id is None or self.duration_seconds is not None:
            raise ValueError("Message actions require messageId and no durationSeconds")
        return self


class JudgmentOutput(Contract):
    judgment: Judgment
    reason_summary: str = Field(min_length=1, max_length=1000)
    evidence_message_ids: list[UUID] = Field(default_factory=list)
    policy_ids: list[str] = Field(default_factory=list)
    requested_actions: list[RequestedAction] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_action_boundary(self):
        if self.judgment == Judgment.NEEDS_REVIEW and self.requested_actions:
            raise ValueError("NEEDS_REVIEW cannot propose mutations")
        if self.judgment == Judgment.NO_VIOLATION and any(
            a.type != "RELEASE_HELD" for a in self.requested_actions
        ):
            raise ValueError("NO_VIOLATION may only propose RELEASE_HELD")
        return self
