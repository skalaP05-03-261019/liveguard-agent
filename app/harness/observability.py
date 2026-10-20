"""실행 추적 (#7). traceId 하나로 노드·도구·토큰·지연·재시도·한도·승인 대기를 남긴다.

- 기본: 구조화 JSON 로그(logger "liveguard.trace"). 채팅 원문·정책 본문·토큰 값은 남기지 않는다.
- LangSmith: LANGSMITH_TRACING=true와 LANGSMITH_API_KEY를 설정하면 LangGraph 실행이 자동 추적된다.
  graph_run_config()로 실행하면 traceId·taskId가 메타데이터로 붙어 같은 ID로 찾을 수 있다.
"""

from collections.abc import Callable
import json
import logging
import os
from typing import Any

logger = logging.getLogger("liveguard.trace")

# 이벤트에 남길 수 있는 필드. 그 밖의 값(원문, 인자, 응답 본문)은 버린다.
_ALLOWED_FIELDS = {
    "traceId", "event", "name", "attempt", "status", "errorCode", "retryable", "latencyMs",
    "delayMs", "reason", "inputTokens", "outputTokens", "outcome", "modelCalls", "toolCalls",
    "activeSeconds", "retryCount", "reviewKind", "reviewId", "decisionId", "threadId", "taskId",
}

Observer = Callable[[dict[str, Any]], None]


class TraceRecorder:
    """이벤트를 메모리에 모으고 JSON 한 줄로 로그에 남긴다."""

    def __init__(self, *, emit_logs: bool = True) -> None:
        self.events: list[dict[str, Any]] = []
        self._emit_logs = emit_logs

    def __call__(self, event: dict[str, Any]) -> None:
        clean = {k: v for k, v in event.items() if k in _ALLOWED_FIELDS and v is not None}
        self.events.append(clean)
        if self._emit_logs:
            logger.info(json.dumps(clean, ensure_ascii=False, sort_keys=True))

    def for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("traceId") == trace_id]


def record_event(observer: Observer | None, trace_id: str, event: str, **fields: Any) -> None:
    """Graph 노드(예: 강퇴 interrupt/resume)가 직접 남기는 이벤트."""
    if observer is not None:
        observer({"traceId": trace_id, "event": event, **fields})


def langsmith_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").lower() == "true" and bool(os.getenv("LANGSMITH_API_KEY"))


def graph_run_config(*, thread_id: str, trace_id: str, task_id: str | None = None) -> dict[str, Any]:
    """LangGraph 실행 config. checkpoint thread와 추적 메타데이터를 함께 지정한다."""
    metadata = {"trace_id": trace_id, "thread_id": thread_id}
    if task_id:
        metadata["task_id"] = task_id
    return {
        "configurable": {"thread_id": thread_id},
        "run_name": "liveguard-agent",
        "tags": ["liveguard", f"trace:{trace_id}"],
        "metadata": metadata,
    }
