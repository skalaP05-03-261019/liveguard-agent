"""Provider-neutral turns; OpenAI is opt-in, never an implicit model choice."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.clients.business_mcp import AgentToolError
from app.runtime.fencing import check_execution, current_lease


@dataclass(frozen=True)
class ModelLimits:
    rpm: int = 30
    tpm: int = 60000
    concurrency: int = 4
    max_input: int = 12000
    max_output: int = 2000
    timeout: float = 30

    def __post_init__(self):
        if (min(self.rpm, self.tpm, self.concurrency, self.max_input, self.max_output) < 1
                or not math.isfinite(self.timeout) or self.timeout <= 0
                or self.tpm < self.max_input + self.max_output):
            raise ValueError("Invalid model limits")

    @classmethod
    def from_env(cls):
        return cls(rpm=int(os.getenv("MODEL_RPM", "30")),
                   tpm=int(os.getenv("MODEL_TPM", "60000")),
                   concurrency=int(os.getenv("MODEL_CONCURRENCY", "4")),
                   max_input=int(os.getenv("MODEL_MAX_INPUT_TOKENS", "12000")),
                   max_output=int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "2000")),
                   timeout=float(os.getenv("MODEL_TIMEOUT_SECONDS", "30")))


def retry_after_seconds(value):
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class ModelRequest:
    """One attempt: admission is outside active execution time, never a DB transaction."""

    def __init__(self, model, messages, tools, *, trace_id):
        self.model, self.messages, self.tools, self.trace_id = model, messages, tools, trace_id
        self.limits = getattr(model, "limits", None) or ModelLimits.from_env()
        self.quota_key = getattr(model, "quota_key", None) or "injected-model"
        # Service estimate, not a vendor tokenizer: include tool schemas and framing.
        text = json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False)
        estimator = getattr(model, "estimate_input_tokens", None)
        self.input_tokens = (estimator(messages, tools) if estimator else
                             math.ceil(sum(2 if ord(c) > 127 else 1 / 3 for c in text)) + 256)
        if type(self.input_tokens) is not int or self.input_tokens < 1:
            raise ValueError("Invalid model input token estimate")
        self.guard = current_lease.get()
        self.reservation = None
        self.result = None

    @asynccontextmanager
    async def admission(self):
        if self.input_tokens > self.limits.max_input:
            if self.guard:
                await asyncio.to_thread(self.guard.inbox.count_event, "model_limits")
            raise AgentToolError("MODEL_CONTEXT_LIMIT", trace_id=self.trace_id)
        if self.guard:
            waited = False
            while True:
                await self.guard.check()
                self.reservation = await asyncio.to_thread(
                    self.guard.inbox.reserve_model, self.guard.job, quota_key=self.quota_key,
                    tokens=self.input_tokens + self.limits.max_output,
                    rpm=self.limits.rpm, tpm=self.limits.tpm, concurrency=self.limits.concurrency)
                if self.reservation:
                    break
                if not waited:
                    await asyncio.to_thread(self.guard.inbox.count_event, "model_waits")
                    waited = True
                await asyncio.sleep(0.25)
        self.result = None
        try:
            await check_execution()
            yield
        finally:
            if self.reservation:
                await asyncio.to_thread(self.guard.inbox.reconcile_model, self.reservation,
                                        getattr(self.result, "usage", None))
                self.reservation = None

    async def __call__(self):
        try:
            async with asyncio.timeout(self.limits.timeout):
                self.result = await self.model.complete(self.messages, self.tools, trace_id=self.trace_id)
            await check_execution()
            usage = getattr(self.result, "usage", None) or {}
            if (usage.get("inputTokens", 0) > self.limits.max_input
                    or usage.get("outputTokens", 0) > self.limits.max_output):
                if self.guard:
                    await asyncio.to_thread(self.guard.inbox.count_event, "model_limits")
                raise AgentToolError("MODEL_TOKEN_LIMIT", trace_id=self.trace_id, usage=usage)
            return self.result
        except TimeoutError:
            if self.guard:
                await asyncio.to_thread(self.guard.inbox.count_event, "model_timeouts")
            raise AgentToolError("MODEL_TIMEOUT", trace_id=self.trace_id, retryable=True) from None
        except AgentToolError as error:
            self.result = error
            if self.guard and error.code == "MODEL_TIMEOUT":
                await asyncio.to_thread(self.guard.inbox.count_event, "model_timeouts")
            if self.guard and error.code == "MODEL_RATE_LIMITED":
                await asyncio.to_thread(self.guard.inbox.model_cooldown,
                                        self.quota_key, max(2, error.retry_after))
            raise


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

    No model default, implicit proxy, automatic provider selection, or credentials in repr.
    Only explicitly configured models supporting function calling can be used.
    """

    def __init__(self, *, model: str, api_key: str,
                 endpoint: str = "https://api.openai.com/v1", provider: str = "openai",
                 quota_group: str | None = None, limits: ModelLimits | None = None):
        if not model.strip() or not api_key.strip():
            raise ValueError("MODEL_NAME and MODEL_API_KEY are required")
        self._model, self._api_key = model, api_key
        from urllib.parse import urlsplit
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Invalid model endpoint")
        if provider != "openai" and not quota_group:
            raise ValueError("Non-OpenAI endpoints require an explicit MODEL_QUOTA_GROUP")
        self._endpoint = endpoint
        self.limits = limits or ModelLimits.from_env()
        identity = ("group", quota_group) if quota_group else (provider, model, endpoint.rstrip("/"))
        self.quota_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    async def complete(self, messages, tools, *, trace_id):
        from openai import AsyncOpenAI, APIStatusError, APITimeoutError, APIConnectionError
        try:
            async with AsyncOpenAI(api_key=self._api_key, max_retries=0, timeout=self.limits.timeout,
                                   base_url=self._endpoint) as client:
                response = await client.chat.completions.create(
                    model=self._model, messages=messages, tools=tools,
                    tool_choice="required", parallel_tool_calls=False,
                    max_completion_tokens=self.limits.max_output, store=False,
                )
        except APIStatusError as error:
            code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN",
                    429: "MODEL_RATE_LIMITED"}.get(error.status_code, "MODEL_UNAVAILABLE")
            raise AgentToolError(code, trace_id=trace_id,
                                 retryable=error.status_code in {429, 500, 502, 503, 504},
                                 retry_after=retry_after_seconds(
                                     error.response.headers.get("Retry-After"))) from None
        except APITimeoutError:
            raise AgentToolError("MODEL_TIMEOUT", trace_id=trace_id, retryable=True) from None
        except APIConnectionError:
            raise AgentToolError("MODEL_UNAVAILABLE", trace_id=trace_id, retryable=True) from None
        usage = response.usage
        usage_dict = {
            "inputTokens": usage.prompt_tokens, "outputTokens": usage.completion_tokens,
        } if usage else {}
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
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id, usage=usage_dict) from None
        return ModelTurn(calls, usage_dict)


def model_from_env() -> InvestigationModel:
    # Explicit opt-in; embedding provider is independent of chat provider.
    provider = os.getenv("MODEL_PROVIDER")
    if provider not in {"openai", "openai-compatible"}:
        raise ValueError("Set MODEL_PROVIDER explicitly or inject InvestigationModel")
    return OpenAIInvestigationModel(model=os.getenv("MODEL_NAME", ""),
                                    api_key=os.getenv("MODEL_API_KEY") or (
                                        os.getenv("OPENAI_API_KEY", "") if provider == "openai" else ""),
                                    provider=provider,
                                    endpoint=os.environ["MODEL_BASE_URL"] if provider != "openai" else
                                        os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1"),
                                    quota_group=os.getenv("MODEL_QUOTA_GROUP"))
