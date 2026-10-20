"""MCP 입력 계약 모델. 외부 JSON은 camelCase, Python 코드는 snake_case를 사용한다."""

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class Contract(BaseModel):
    # 계약에 없는 필드는 조용히 무시하지 않고 거부해 BE/Agent 간 불일치를 드러낸다.
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ActionCommand(Contract):
    # Backend가 검증·발급한 명령만 받는다. Agent가 조치 조건을 임의로 만들지 않는다.
    action_id: UUID
    plan_id: UUID
    case_id: UUID
    type: Literal["HIDE", "TIMEOUT", "RELEASE_HELD", "KICK"]
    target_id: UUID
    expected_evidence_version: int = Field(ge=1)
    expected_target_version: int = Field(ge=1)
    policy_version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    duration_seconds: int | None = Field(default=None, ge=1)
    decision_id: UUID | None = None
    violation_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_duration(self) -> "ActionCommand":
        # 제재 기간 산정은 Backend 책임이다. TIMEOUT 외 조치에는 기간을 허용하지 않는다.
        if self.type == "TIMEOUT" and self.duration_seconds is None:
            raise ValueError("TIMEOUT requires a Backend-issued durationSeconds")
        if self.type != "TIMEOUT" and self.duration_seconds is not None:
            raise ValueError("durationSeconds is only valid for TIMEOUT")
        if self.type == "KICK":
            if self.decision_id is None:
                raise ValueError("KICK requires an approved decisionId")
            if len(set(self.violation_ids)) < 3:
                raise ValueError("KICK requires at least three distinct violationIds")
        elif self.decision_id is not None or self.violation_ids:
            raise ValueError("decisionId and violationIds are only valid for KICK")
        return self


class HideCommand(ActionCommand):
    # MCP tool마다 허용 조치 종류를 입력 스키마 단계에서 제한한다.
    type: Literal["HIDE"]


class TimeoutCommand(ActionCommand):
    type: Literal["TIMEOUT"]


class ReleaseHeldCommand(ActionCommand):
    type: Literal["RELEASE_HELD"]


class KickCommand(ActionCommand):
    # 승인된 운영자 결정과 누적 위반 참조를 포함한 Backend 발급 명령만 받는다.
    type: Literal["KICK"]
    decision_id: UUID
    violation_ids: list[UUID] = Field(min_length=3)


class BackendError(Exception):
    """Backend 오류를 MCP tool 오류 envelope로 변환하기 위한 내부 표현."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        trace_id: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.trace_id = trace_id

    def as_tool_error(self) -> str:
        return json.dumps(
            {
                "error": {
                    "code": self.code,
                    "message": self.message,
                    "retryable": self.retryable,
                },
                "traceId": self.trace_id,
            },
            ensure_ascii=False,
        )
