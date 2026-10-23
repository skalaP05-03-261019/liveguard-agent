"""Agent-internal summary contract (#72). Not a BE/FE public API; field changes affect the input adapter."""

from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from app.schemas import Contract

MAX_MESSAGES = 100
# Provisional internal threshold, not a service policy. Tune with the evaluation record.
MIN_CONTENT_MESSAGES = 3


class SummaryStatus(StrEnum):
    SUCCESS = "SUCCESS"
    INSUFFICIENT_CHAT = "INSUFFICIENT_CHAT"
    MODEL_ERROR = "MODEL_ERROR"
    TIMEOUT = "TIMEOUT"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


class SummaryMessage(Contract):
    message_id: UUID
    live_id: UUID
    text: str = Field(min_length=1, max_length=500)
    sent_at: AwareDatetime
    # Last-line check only: the server must already have excluded HELD/HIDDEN.
    visibility: str
    version: int = Field(ge=1)

    @field_validator("visibility")
    @classmethod
    def public_only(cls, value):
        if value != "PUBLIC":
            raise ValueError("Only PUBLIC messages can be summarized")
        return value

    @field_validator("sent_at")
    @classmethod
    def as_utc(cls, value):
        return _utc(value)


class SummaryInput(Contract):
    live_id: UUID
    window_start: AwareDatetime
    window_end: AwareDatetime
    messages: list[SummaryMessage] = Field(default_factory=list, max_length=MAX_MESSAGES)
    source_version: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("window_start", "window_end")
    @classmethod
    def as_utc(cls, value):
        return _utc(value)

    @model_validator(mode="after")
    def same_live_and_window(self):
        if self.window_start >= self.window_end:
            raise ValueError("windowStart must be before windowEnd")
        ids = [m.message_id for m in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate messageId")
        for message in self.messages:
            if message.live_id != self.live_id:
                raise ValueError("Message from another live")
            if not self.window_start <= message.sent_at < self.window_end:
                raise ValueError("Message outside [windowStart, windowEnd)")
        return self

    def content_messages(self) -> list[SummaryMessage]:
        """Messages with text, in time order. Blank messages never reach the model or evidence."""
        return sorted((m for m in self.messages if m.text.strip()),
                      key=lambda m: (m.sent_at, str(m.message_id)))

    def fingerprint(self) -> str:
        """Reproducible cache key part from window, IDs, versions and text hashes (no raw text)."""
        canonical = {
            "liveId": str(self.live_id), "windowStart": self.window_start.isoformat(),
            "windowEnd": self.window_end.isoformat(), "sourceVersion": self.source_version,
            "messages": sorted([str(m.message_id), m.version, hashlib.sha256(m.text.encode()).hexdigest()]
                               for m in self.messages),
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


class SummaryItem(Contract):
    text: str = Field(min_length=1, max_length=200)
    evidence_message_ids: list[UUID] = Field(min_length=1, max_length=10)


class SummaryResult(Contract):
    status: SummaryStatus
    live_id: UUID
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    source_fingerprint: str
    source_version: str | None = None
    summary_version: str
    model_version: str | None = None
    # Machine code for logs/consumers only; never shown as summary text.
    error_code: str | None = None
    topics: list[SummaryItem] = Field(default_factory=list)
    questions_or_suggestions: list[SummaryItem] = Field(default_factory=list)
    unresolved_items: list[SummaryItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def items_only_on_success(self):
        has_items = self.topics or self.questions_or_suggestions or self.unresolved_items
        if self.status != SummaryStatus.SUCCESS and has_items:
            raise ValueError("Only a successful summary carries items")
        return self

    def evidence_ids(self) -> set[UUID]:
        """All cited messages, for invalidation when one is hidden, deleted or edited."""
        return {mid for items in (self.topics, self.questions_or_suggestions, self.unresolved_items)
                for item in items for mid in item.evidence_message_ids}
