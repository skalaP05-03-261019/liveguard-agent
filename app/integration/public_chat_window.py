"""Validate a BE-authorized public chat window before it reaches a summarizer.

This is a defense-in-depth input contract, not caller authorization or a BE
lookup. The caller must obtain the complete PUBLIC-only window through an
authenticated Backend contract; that provider API is still to be agreed.
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas import Contract


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Chat summary timestamps must include a timezone")
    return value.astimezone(timezone.utc)


class PublicChatMessage(Contract):
    message_id: UUID
    live_id: UUID
    text: str = Field(min_length=1, max_length=500, strict=True)
    sent_at: datetime
    visibility: Literal["PUBLIC"]
    version: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def valid_text_and_time(self):
        if not self.text.strip():
            raise ValueError("Public chat text must not be blank")
        _utc(self.sent_at)
        return self


class PublicChatWindow(Contract):
    live_id: UUID
    window_start: datetime
    window_end: datetime
    # A partial page must never be presented as the whole requested interval.
    complete: Literal[True]
    messages: list[PublicChatMessage] = Field(max_length=100)
    source_version: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def valid_scope(self):
        start, end = _utc(self.window_start), _utc(self.window_end)
        if start >= end:
            raise ValueError("Chat summary window must have a positive interval")
        ids = set()
        for message in self.messages:
            if message.live_id != self.live_id:
                raise ValueError("Public chat window contains another live")
            if message.message_id in ids:
                raise ValueError("Public chat window contains a duplicate message")
            ids.add(message.message_id)
            if not start <= _utc(message.sent_at) < end:
                raise ValueError("Public chat message is outside the requested window")
        return self

    def ordered_messages(self) -> tuple[PublicChatMessage, ...]:
        return tuple(sorted(self.messages, key=lambda item: (
            _utc(item.sent_at), str(item.message_id))))

    def source_fingerprint(self) -> str:
        """Stable digest for change detection; never log the unhashed payload."""
        payload = {
            "liveId": str(self.live_id),
            "windowStart": _utc(self.window_start).isoformat(),
            "windowEnd": _utc(self.window_end).isoformat(),
            "sourceVersion": self.source_version,
            "messages": [
                {"messageId": str(message.message_id),
                 "sentAt": _utc(message.sent_at).isoformat(),
                 "version": message.version, "text": message.text}
                for message in self.ordered_messages()
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
