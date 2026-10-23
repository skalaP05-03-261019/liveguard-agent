"""Summarize authorized public chat with one bounded model tool call; never invent success."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable
from uuid import UUID

from pydantic import Field, ValidationError

from app.clients.business_mcp import AgentToolError
from app.investigation.model import InvestigationModel, ModelRequest
from app.harness.safety import safe_presentation
from app.schemas import Contract
from .schemas import MIN_CONTENT_MESSAGES, SummaryInput, SummaryItem, SummaryResult, SummaryStatus

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "chat-summary.md"
# Prompt changes change the version, so cached summaries from an older prompt are not reused.
SUMMARY_VERSION = "chat-summary-v1+" + hashlib.sha256(PROMPT.read_bytes()).hexdigest()[:12]
MAX_ITEMS = 5
MAX_ATTEMPTS = 2  # one correction turn after a rejected submission
TIMEOUT_SECONDS = 60


class SubmitSummary(Contract):
    topics: list[SummaryItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    questions_or_suggestions: list[SummaryItem] = Field(default_factory=list, max_length=MAX_ITEMS)
    unresolved_items: list[SummaryItem] = Field(default_factory=list, max_length=MAX_ITEMS)


def tool_definitions():
    return [{"type": "function", "function": {
        "name": "submit_summary",
        "description": "채팅 요약 제출. 각 항목은 짧은 문장과 근거 messageId 목록.",
        "parameters": SubmitSummary.model_json_schema(by_alias=True), "strict": False}}]


class _Rejected(Exception):
    def __init__(self, code):
        self.code = code


def _parse(call, known: set[UUID]) -> SubmitSummary:
    if call.name != "submit_summary":
        raise _Rejected("UNKNOWN_TOOL")
    try:
        submitted = SubmitSummary.model_validate(call.arguments)
    except ValidationError:
        raise _Rejected("INVALID_SUMMARY_FORMAT") from None
    for items in (submitted.topics, submitted.questions_or_suggestions, submitted.unresolved_items):
        for item in items:
            refs = item.evidence_message_ids
            if not item.text.strip() or len(refs) != len(set(refs)) or not set(refs) <= known:
                raise _Rejected("INVALID_EVIDENCE")
    return submitted


async def summarize(request: SummaryInput, *, model: InvestigationModel | None, trace_id: str,
                    model_version: str | None = None, timeout_seconds: float = TIMEOUT_SECONDS,
                    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> SummaryResult:
    """Return a SummaryResult. Model failures become MODEL_ERROR/TIMEOUT, never a partial summary."""
    base = {"live_id": request.live_id, "window_start": request.window_start,
            "window_end": request.window_end, "source_fingerprint": request.fingerprint(),
            "source_version": request.source_version, "summary_version": SUMMARY_VERSION}
    messages = request.content_messages()
    if len(messages) < MIN_CONTENT_MESSAGES:
        return SummaryResult(status=SummaryStatus.INSUFFICIENT_CHAT, generated_at=now(), **base)
    if model is None:
        return SummaryResult(status=SummaryStatus.MODEL_ERROR, error_code="MODEL_NOT_CONFIGURED",
                             generated_at=now(), **base)
    base["model_version"] = model_version
    known = {m.message_id for m in messages}
    data = {"dataKind": "UNTRUSTED_CHAT_DATA", "windowStart": request.window_start.isoformat(),
            "windowEnd": request.window_end.isoformat(),
            "messages": [{"messageId": str(m.message_id), "sentAt": m.sent_at.isoformat(), "text": m.text}
                         for m in messages]}
    conversation = [{"role": "system", "content": PROMPT.read_text(encoding="utf-8")},
                    {"role": "user", "content": json.dumps(safe_presentation(data), ensure_ascii=False)}]
    tools = tool_definitions()
    code = "INVALID_MODEL_OUTPUT"
    try:
        async with asyncio.timeout(timeout_seconds):
            for _ in range(MAX_ATTEMPTS):
                request_ = ModelRequest(model, list(conversation), tools, trace_id=trace_id)
                async with request_.admission():
                    turn = await request_()
                if len(turn.calls) != 1:
                    code = "INVALID_MODEL_OUTPUT"
                    break
                call = turn.calls[0]
                try:
                    submitted = _parse(call, known)
                except _Rejected as rejected:
                    code = rejected.code
                    # Tell the model why, once; the reason is a fixed code, not model or chat text.
                    conversation += [
                        {"role": "assistant", "content": None, "tool_calls": [{
                            "id": call.id, "type": "function", "function": {
                                "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}]},
                        {"role": "tool", "tool_call_id": call.id, "content": json.dumps(
                            {"error": code, "rule": "각 항목의 evidenceMessageIds는 입력 messageId만, 1~10개, 중복 없이"},
                            ensure_ascii=False)}]
                    continue
                return SummaryResult(status=SummaryStatus.SUCCESS, generated_at=now(), **base,
                                     topics=submitted.topics,
                                     questions_or_suggestions=submitted.questions_or_suggestions,
                                     unresolved_items=submitted.unresolved_items)
    except TimeoutError:
        return SummaryResult(status=SummaryStatus.TIMEOUT, error_code="SUMMARY_TIMEOUT", generated_at=now(), **base)
    except AgentToolError as error:
        if error.code == "MODEL_TIMEOUT":
            return SummaryResult(status=SummaryStatus.TIMEOUT, error_code=error.code, generated_at=now(), **base)
        code = error.code
    except Exception:
        # Summary is optional: an unexpected provider failure must not break chat or moderation.
        code = "SUMMARY_ERROR"
    return SummaryResult(status=SummaryStatus.MODEL_ERROR, error_code=code, generated_at=now(), **base)


def is_stale(request: SummaryInput, message_id: UUID, version: int) -> bool:
    """A visibility/content change of any input message makes the cached summary unusable.

    Consumers call this on BE visibility-change events (messageId, visibility, version);
    the summary was built from that message even if it is not cited.
    """
    for message in request.messages:
        if message.message_id == message_id:
            return version != message.version
    return False
