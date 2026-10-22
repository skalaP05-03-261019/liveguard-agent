"""실행 한도와 오류 복구 (#6). 조사 루프의 ExecutionGate(app.investigation.runner)를 구현한다.

- 한도: 모델 호출 8회, 같은 도구 3회, 활성 실행 120초 (수업 Harness 표 6-3, 기획서 10.1).
- 재시도: retryable로 표시된 일시 오류(429·503·타임아웃 등)만 호출당 최대 2회. 재시도도 호출 수에 포함한다.
- 지속: 카운터를 State `harness_budget`에 저장해 재시작·재개·재전달로 예산이 초기화되지 않게 한다.
  운영자 승인 대기는 연산이 실행되는 시간이 아니므로 활성 시간에 포함되지 않는다.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time
from typing import Any

from app.clients.business_mcp import AgentToolError
from app.harness.observability import Observer, TraceRecorder, record_event
from app.harness.safety import safe_presentation
from app.runtime.fencing import check_execution

EXECUTION_LIMIT = "EXECUTION_LIMIT"
MODEL = "model"
_DEFAULT_RECORDER = TraceRecorder()


def default_observer() -> Observer:
    # 프로세스 공용 기록기. 이벤트는 JSON 로그로 남고 traceId로 모아 볼 수 있다.
    return _DEFAULT_RECORDER


@dataclass(frozen=True)
class ExecutionLimits:
    max_model_calls: int = 8
    max_same_tool_calls: int = 3
    max_active_seconds: float = 120.0
    max_retries_per_call: int = 2
    backoff_seconds: tuple[float, ...] = (0.5, 1.0)


class HarnessGate:
    """Graph 전체가 공유하는 단 하나의 gate. 다른 gate나 SDK 재시도로 감싸지 않는다."""

    def __init__(
        self,
        *,
        trace_id: str,
        budget: dict[str, Any] | None = None,
        limits: ExecutionLimits = ExecutionLimits(),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        observer: Observer | None = None,
    ) -> None:
        budget = budget or {}
        self.trace_id = trace_id
        self.limits = limits
        self._clock = clock
        self._sleep = sleep
        self.observer = observer
        self.model_calls = int(budget.get("modelCalls", 0))
        self.tool_calls: dict[str, int] = dict(budget.get("toolCalls", {}))
        self.active_seconds = float(budget.get("activeSeconds", 0.0))
        self.retry_count = int(budget.get("retryCount", 0))
        self.exhausted: str | None = budget.get("exhausted")
        self.last_error: dict[str, Any] | None = budget.get("lastError")

    @classmethod
    def from_state(cls, state: dict[str, Any], **kwargs: Any) -> "HarnessGate":
        return cls(trace_id=state.get("trace_id", ""), budget=state.get("harness_budget"), **kwargs)

    def snapshot(self) -> dict[str, Any]:
        """State `harness_budget`에 저장할 값. 원문·토큰은 넣지 않는다."""
        return {
            "modelCalls": self.model_calls,
            "toolCalls": dict(self.tool_calls),
            "activeSeconds": round(self.active_seconds, 3),
            "retryCount": self.retry_count,
            "exhausted": self.exhausted,
            "lastError": self.last_error,
            "limits": {
                "maxModelCalls": self.limits.max_model_calls,
                "maxSameToolCalls": self.limits.max_same_tool_calls,
                "maxActiveSeconds": self.limits.max_active_seconds,
                "maxRetriesPerCall": self.limits.max_retries_per_call,
            },
        }

    def _emit(self, event: str, **fields: Any) -> None:
        record_event(self.observer, self.trace_id, event, **fields)

    def _elapsed_ms(self, started: float) -> int:
        return int((self._clock() - started) * 1000)

    def _limit(self, reason: str) -> AgentToolError:
        self.exhausted = reason
        self.last_error = {"code": EXECUTION_LIMIT, "reason": reason, "retryable": False}
        return AgentToolError(EXECUTION_LIMIT, trace_id=self.trace_id)

    def _count_attempt(self, name: str) -> None:
        # 한 번의 시도마다 센다. 재시도도 같은 예산을 쓴다.
        if self.exhausted:
            raise self._limit(self.exhausted)
        if name == MODEL:
            if self.model_calls + 1 > self.limits.max_model_calls:
                raise self._limit("MODEL_CALLS")
            self.model_calls += 1
        else:
            used = self.tool_calls.get(name, 0)
            if used + 1 > self.limits.max_same_tool_calls:
                raise self._limit(f"TOOL_CALLS:{name}")
            self.tool_calls[name] = used + 1

    async def _timed(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        remaining = self.limits.max_active_seconds - self.active_seconds
        if remaining <= 0:
            raise self._limit("ACTIVE_TIME")
        started = self._clock()
        try:
            async with asyncio.timeout(remaining):
                return await operation()
        except TimeoutError:
            raise self._limit("ACTIVE_TIME") from None
        finally:
            self.active_seconds += self._clock() - started

    async def run(self, name: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        retries = 0
        while True:
            await check_execution()
            try:
                self._count_attempt(name)
            except AgentToolError:
                self._emit("limit", name=name, reason=self.exhausted)
                raise
            started = self._clock()
            try:
                result = await self._timed(operation)
                await check_execution()
            except AgentToolError as error:
                self._emit("call", name=name, attempt=retries + 1, status="error", errorCode=error.code,
                           retryable=error.retryable, latencyMs=self._elapsed_ms(started))
                if error.code == EXECUTION_LIMIT:
                    self._emit("limit", name=name, reason=self.exhausted)
                    raise
                self.last_error = {"code": error.code, "operation": name, "retryable": error.retryable}
                if not error.retryable or retries >= self.limits.max_retries_per_call:
                    raise
            else:
                usage = getattr(result, "usage", None) or {}
                self._emit("call", name=name, attempt=retries + 1, status="ok",
                           latencyMs=self._elapsed_ms(started),
                           inputTokens=usage.get("inputTokens"), outputTokens=usage.get("outputTokens"))
                return result
            retries += 1
            self.retry_count += 1
            delay = self.limits.backoff_seconds[min(retries, len(self.limits.backoff_seconds)) - 1]
            self._emit("retry", name=name, attempt=retries + 1, delayMs=int(delay * 1000))
            started = self._clock()
            await self._sleep(delay)
            # 재시도 대기도 작업이 진행 중인 시간이므로 활성 시간에 포함한다.
            self.active_seconds += self._clock() - started


def task_outcome(error: AgentToolError) -> str:
    """한도 초과는 신규 조치 없이 운영자 검토로, 그 외 최종 오류는 FAILED로 끝낸다."""
    return "NEEDS_REVIEW" if error.code == EXECUTION_LIMIT else "FAILED"


async def run_guarded(
    state: dict[str, Any],
    step: Callable[[HarnessGate], Awaitable[dict[str, Any]]],
    *,
    gate: HarnessGate | None = None,
) -> dict[str, Any]:
    """노드 하나를 공유 gate로 실행하고, 성공·실패 모두 예산을 State에 남기는 patch를 돌려준다.

    실패해도 예외로 끝내지 않는다. 노드가 예외로 끝나면 사용한 예산이 checkpoint에 남지 않아
    재실행 시 예산이 초기화되기 때문이다. 실패 patch에는 판정이나 조치를 넣지 않는다.
    """
    active_gate = gate or HarnessGate.from_state(state)
    try:
        patch = await step(active_gate)
    except AgentToolError as error:
        outcome = task_outcome(error)
        budget = active_gate.snapshot()
        active_gate._emit("outcome", outcome=outcome, errorCode=error.code, **_budget_fields(budget))
        return {
            "harness_budget": budget,
            "task_outcome": outcome,
            "error": {"code": error.code, "retryable": error.retryable, "traceId": error.trace_id},
        }
    budget = active_gate.snapshot()
    active_gate._emit("outcome", outcome="SUCCEEDED", **_budget_fields(budget))
    return {**patch, "harness_budget": budget}


def _budget_fields(budget: dict[str, Any]) -> dict[str, Any]:
    return {key: budget[key] for key in ("modelCalls", "toolCalls", "activeSeconds", "retryCount")}


async def guarded_investigate(
    state: dict[str, Any], *, observer: Observer | None = None, **deps: Any
) -> dict[str, Any]:
    """조사 노드(#9)를 공유 gate·안전장치·추적으로 실행한다. Graph 연결(#10)은 nodes.investigate 대신 이 함수를 쓴다."""
    from app import nodes

    deps.setdefault("present_data", safe_presentation)
    gate = HarnessGate.from_state(state, observer=observer or default_observer())
    return await run_guarded(state, lambda g: nodes.investigate(state, gate=g, **deps), gate=gate)


async def resolve_unknown_action(
    result: dict[str, Any], *, read_status: Callable[[str], Awaitable[dict[str, Any]]]
) -> dict[str, Any]:
    """조치 결과가 UNKNOWN이면 다시 실행하지 않고 get_action_status로 실제 상태를 확인한다."""
    if result.get("status") != "UNKNOWN" or not result.get("actionId"):
        return result
    observed = await read_status(result["actionId"])
    return {**result, "status": observed.get("status", "UNKNOWN"), "confirmedBy": "get_action_status"}
