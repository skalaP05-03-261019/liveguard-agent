"""Typed MCP inputs. JSON fields use camelCase to match the integration contract."""

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class ActionCommand(Contract):
    action_id: UUID
    plan_id: UUID
    case_id: UUID
    type: Literal["HIDE", "TIMEOUT", "RELEASE_HELD"]
    target_id: UUID
    expected_evidence_version: int = Field(ge=1)
    expected_target_version: int = Field(ge=1)
    policy_version: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    duration_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_duration(self) -> "ActionCommand":
        if self.type == "TIMEOUT" and self.duration_seconds is None:
            raise ValueError("TIMEOUT requires a Backend-issued durationSeconds")
        if self.type != "TIMEOUT" and self.duration_seconds is not None:
            raise ValueError("durationSeconds is only valid for TIMEOUT")
        return self


class HideCommand(ActionCommand):
    type: Literal["HIDE"]


class TimeoutCommand(ActionCommand):
    type: Literal["TIMEOUT"]


class ReleaseHeldCommand(ActionCommand):
    type: Literal["RELEASE_HELD"]


class BackendError(Exception):
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
