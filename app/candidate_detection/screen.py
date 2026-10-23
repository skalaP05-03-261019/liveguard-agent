"""One bounded, model-injected candidate assessment; no HTTP routes, DB or business tools."""

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError
from app.clients.business_mcp import AgentToolError
from app.investigation.model import InvestigationModel, ModelTurn
from .contracts import Assessment, ScreeningInput, validate_evidence

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "candidate-screen.md"


def tool_definition(data: ScreeningInput):
    schema = Assessment.model_json_schema(by_alias=True)
    # The model selects references; the server restores exact supplied chat text.
    # Asking the model to copy the text makes a harmless paraphrase a format error.
    quote = schema["$defs"]["Quote"]
    quote["properties"]["messageId"]["enum"] = [
        str(m.message_id) for m in [data.target, *data.context]]
    quote["properties"].pop("text")
    quote["required"].remove("text")
    return {"type": "function", "function": {
        "name": "report_candidate_signals",
        "description": "Report TARGET risk signals with supplied messageId references only; no action.",
        "parameters": schema, "strict": False,
    }}


def restore_evidence_text(data: ScreeningInput, arguments: dict) -> dict:
    """Fill missing quote text from validated input IDs; never trust a model paraphrase."""
    result = deepcopy(arguments)
    if not isinstance(result, dict):
        return result
    messages = {m.message_id: m.text for m in [data.target, *data.context]}
    signals = result.get("signals", [])
    if not isinstance(signals, list):
        return result
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        evidence = signal.get("evidence", [])
        if not isinstance(evidence, list):
            continue
        for quote in evidence:
            if not isinstance(quote, dict) or "text" in quote:
                continue
            reference = quote.get("messageId")
            if type(reference) is not str:
                continue
            try:
                message_id = UUID(reference)
            except ValueError:
                continue
            if message_id in messages:
                quote["text"] = messages[message_id]
    return result


class CandidateScreen:
    def __init__(self, model: InvestigationModel, *, timeout_seconds: float = 8):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("Timeout must be finite and in (0, 30]")
        self.model, self.timeout_seconds = model, timeout_seconds

    async def assess(self, data: ScreeningInput, *, trace_id: str):
        trace_id = str(UUID(trace_id))
        messages = [{"role": "system", "content": PROMPT.read_text()},
                    {"role": "user", "content": json.dumps({
                        "dataKind": "UNTRUSTED_CHAT_DATA",
                        "input": data.model_dump(mode="json", by_alias=True)}, ensure_ascii=False)}]
        try:
            async with asyncio.timeout(self.timeout_seconds):
                turn = await self.model.complete(messages, [tool_definition(data)], trace_id=trace_id)
        except TimeoutError:
            raise AgentToolError("CANDIDATE_TIMEOUT", trace_id=trace_id, retryable=True) from None
        if not isinstance(turn, ModelTurn) or len(turn.calls) != 1:
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id)
        call = turn.calls[0]
        if call.name != "report_candidate_signals":
            raise AgentToolError("FORBIDDEN", trace_id=trace_id)
        try:
            assessment = validate_evidence(
                data, Assessment.model_validate(restore_evidence_text(data, call.arguments)))
        except (ValidationError, ValueError, TypeError):
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id) from None
        usage = {k: v for k, v in turn.usage.items()
                 if k in {"inputTokens", "outputTokens"} and type(v) is int and v >= 0}
        return assessment, usage
