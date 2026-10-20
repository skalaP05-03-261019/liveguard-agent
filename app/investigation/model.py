"""Provider-neutral turns; OpenAI is opt-in, never an implicit model choice."""

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.clients.business_mcp import AgentToolError


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelTurn:
    calls: list[ToolCall]
    usage: dict[str, int] = field(default_factory=dict)


class InvestigationModel(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict], *, trace_id: str) -> ModelTurn: ...


class OpenAIInvestigationModel:
    """Chat Completions function calling. SDK retries off; #6 owns recovery.

    No model default, proxy URL, automatic provider selection, or credentials in repr.
    Only explicitly configured models supporting function calling can be used.
    """

    def __init__(self, *, model: str, api_key: str):
        if not model.strip() or not api_key.strip():
            raise ValueError("MODEL_NAME and MODEL_API_KEY are required")
        self._model, self._api_key = model, api_key

    async def complete(self, messages, tools, *, trace_id):
        from openai import AsyncOpenAI, APIStatusError, APITimeoutError, APIConnectionError
        try:
            async with AsyncOpenAI(api_key=self._api_key, max_retries=0, timeout=30.0,
                                   base_url="https://api.openai.com/v1") as client:
                response = await client.chat.completions.create(
                    model=self._model, messages=messages, tools=tools,
                    tool_choice="required", parallel_tool_calls=False,
                    max_completion_tokens=3000, store=False,
                )
        except APIStatusError as error:
            code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN"}.get(error.status_code, "MODEL_UNAVAILABLE")
            raise AgentToolError(code, trace_id=trace_id,
                                 retryable=error.status_code in {429, 503}) from None
        except (APITimeoutError, APIConnectionError):
            raise AgentToolError("MODEL_UNAVAILABLE", trace_id=trace_id, retryable=True) from None
        # Refusals, token cutoffs and empty answers cannot become moderation findings.
        try:
            choice = response.choices[0]
            if choice.finish_reason != "tool_calls" or choice.message.refusal:
                raise ValueError
            calls = []
            for call in choice.message.tool_calls or []:
                if call.type != "function" or len(call.function.arguments) > 32_000:
                    raise ValueError
                args = json.loads(call.function.arguments)
                if not isinstance(args, dict):
                    raise ValueError
                calls.append(ToolCall(call.id, call.function.name, args))
            if len(calls) != 1:
                raise ValueError
        except (ValueError, TypeError, IndexError, AttributeError):
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id) from None
        usage = response.usage
        return ModelTurn(calls, {
            "inputTokens": usage.prompt_tokens, "outputTokens": usage.completion_tokens,
        } if usage else {})


def model_from_env() -> InvestigationModel:
    # Explicit opt-in; embedding provider is independent of chat provider.
    if os.getenv("MODEL_PROVIDER") != "openai":
        raise ValueError("Set MODEL_PROVIDER explicitly or inject InvestigationModel")
    return OpenAIInvestigationModel(model=os.getenv("MODEL_NAME", ""),
                                    api_key=os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY", ""))
