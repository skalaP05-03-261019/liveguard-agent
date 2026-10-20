"""같은 thread_id로 운영자 결정을 넣어 Graph를 재개한다. 중복 결정은 다시 실행하지 않는다."""

from typing import Any
from uuid import UUID

from langgraph.types import Command

from app.harness.kick_review import KICK_APPROVAL_REQUIRED


class ResumeError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def pending_kick_approval(graph, thread_id: str) -> dict[str, Any] | None:
    """재시작 후에도 체크포인트에서 대기 중인 강퇴 승인 요청을 읽는다."""
    snapshot = await graph.aget_state(thread_config(thread_id))
    for item in snapshot.interrupts:
        value = item.value
        if isinstance(value, dict) and value.get("type") == KICK_APPROVAL_REQUIRED:
            return value
    return None


async def resume_kick_review(
    graph, *, thread_id: str, review_id: UUID, decision_id: UUID, case_id: UUID
) -> dict[str, Any]:
    snapshot = await graph.aget_state(thread_config(thread_id))
    if snapshot.created_at is None:
        raise ResumeError("THREAD_NOT_FOUND", "저장된 작업이 없다", status_code=404)
    pending = await pending_kick_approval(graph, thread_id)
    if pending is None:
        # 이미 재개됐거나 승인 대기가 아닌 thread. 같은 결정이 두 번 와도 강퇴를 반복하지 않는다.
        result = snapshot.values.get("kick_result")
        raise ResumeError(
            "NO_PENDING_APPROVAL",
            "승인 대기 중인 강퇴 심사가 없다" + (f" (이미 {result.get('status')})" if result else ""),
            status_code=409,
        )
    if pending.get("caseId") != str(case_id):
        raise ResumeError("CASE_MISMATCH", "요청 사건과 대기 중인 사건이 다르다", status_code=409)
    if pending.get("reviewId") != str(review_id):
        # 같은 Graph의 다른 심사(예: 이전 문맥 결정)와 혼동하지 않는다.
        raise ResumeError("REVIEW_MISMATCH", "대기 중인 강퇴 심사와 reviewId가 다르다", status_code=409)

    final_state = await graph.ainvoke(
        Command(resume={"reviewId": str(review_id), "decisionId": str(decision_id)}),
        thread_config(thread_id),
    )
    return final_state.get("kick_result", {})
