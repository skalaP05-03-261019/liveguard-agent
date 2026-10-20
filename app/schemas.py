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
    kind: Literal["ANALYZE", "REANALYZE", "RESUME_REVIEW", "RECONCILE"]
    thread_id: str = Field(min_length=1)
    snapshot_id: UUID
    expected_evidence_version: int = Field(ge=1)
    decision_id: UUID | None = None
    # CONTRACTS §11 재개 envelope. 문맥 결정(CONTEXT)과 강퇴 승인(KICK)을 구분한다.
    review_kind: Literal["CONTEXT", "KICK"] | None = None
    review_id: UUID | None = None
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


class Category(StrEnum):
    PROFANITY = "PROFANITY"
    HARASSMENT = "HARASSMENT"
    FLOOD = "FLOOD"
    PHISHING = "PHISHING"
    PII_EXPOSURE = "PII_EXPOSURE"


class MessageFinding(Contract):
    """Notion CONTRACTS 출력 제안. 제재 기간과 실행 명령은 포함하지 않는다."""

    message_id: UUID
    verdict: Judgment
    categories: list[Category] = Field(default_factory=list)
    policy_ids: list[str] = Field(default_factory=list)
    evidence_message_ids: list[UUID] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=1000)
    missing_facts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_finding(self):
        if len(self.categories) != len(set(self.categories)):
            raise ValueError("categories must be unique")
        if self.verdict == Judgment.VIOLATION and not (
            self.categories and self.policy_ids and self.evidence_message_ids
        ):
            raise ValueError("VIOLATION requires category, policy and evidence")
        if self.verdict == Judgment.NO_VIOLATION and self.categories:
            raise ValueError("NO_VIOLATION cannot assign violation categories")
        if self.verdict == Judgment.NEEDS_REVIEW and not self.missing_facts:
            raise ValueError("NEEDS_REVIEW requires missingFacts")
        return self


class AnalysisResult(Contract):
    """다음 PR의 조사 루프가 생성할 메시지별 출력. 아직 HTTP 결과로 반환하지 않는다."""

    case_id: UUID
    snapshot_id: UUID
    evidence_version: int = Field(ge=1)
    policy_version: str = Field(min_length=1)
    message_findings: list[MessageFinding] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_messages(self):
        ids = [finding.message_id for finding in self.message_findings]
        if len(ids) != len(set(ids)):
            raise ValueError("messageFindings must contain unique message IDs")
        return self


class RequestedAction(Contract):
    # Legacy v0.3 scaffold only; AnalysisResult does not use this model.
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
