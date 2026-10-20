"""같은 thread_id로 운영자 결정을 넣어 Graph를 재개한다. 중복 결정은 다시 실행하지 않는다.

한 thread에서 문맥 결정(CONTEXT)과 후속 강퇴 결정(KICK)이 차례로 기다릴 수 있으므로
대기 중인 심사의 종류·reviewId가 요청과 같을 때만 재개한다(CONTRACTS §11).
"""

from typing import Any
from uuid import UUID

from langgraph.types import Command

from app.harness.context_review import CONTEXT_DECISION_REQUIRED
from app.harness.kick_review import KICK_APPROVAL_REQUIRED

# reviewKind → (interrupt payload type, 결과 State 필드)
REVIEW_KINDS = {
    "KICK": (KICK_APPROVAL_REQUIRED, "kick_result"),
    "CONTEXT": (CONTEXT_DECISION_REQUIRED, "context_result"),
}
_PENDING_TYPES = {payload_type: kind for kind, (payload_type, _) in REVIEW_KINDS.items()}


class ResumeError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def thread_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def pending_review(graph, thread_id: str, review_kind: str | None = None) -> dict[str, Any] | None:
    """재시작 후에도 체크포인트에서 대기 중인 심사 요청(운영자 카드)을 읽는다."""
    snapshot = await graph.aget_state(thread_config(thread_id))
    for item in snapshot.interrupts:
        value = item.value
        if isinstance(value, dict) and value.get("type") in _PENDING_TYPES:
            if review_kind is None or _PENDING_TYPES[value["type"]] == review_kind:
                return value
    return None


async def pending_kick_approval(graph, thread_id: str) -> dict[str, Any] | None:
    return await pending_review(graph, thread_id, "KICK")


async def pending_context_review(graph, thread_id: str) -> dict[str, Any] | None:
    return await pending_review(graph, thread_id, "CONTEXT")


async def resume_review(
    graph, *, review_kind: str, thread_id: str, review_id: UUID, decision_id: UUID, case_id: UUID
) -> dict[str, Any]:
    """대기 중인 심사를 결정 참조로 재개하고 해당 결과(kick_result / context_result)를 돌려준다."""
    if review_kind not in REVIEW_KINDS:
        raise ResumeError("UNSUPPORTED_REVIEW_KIND", "reviewKind는 CONTEXT 또는 KICK", status_code=400)
    result_key = REVIEW_KINDS[review_kind][1]
    snapshot = await graph.aget_state(thread_config(thread_id))
    if snapshot.created_at is None:
        raise ResumeError("THREAD_NOT_FOUND", "저장된 작업이 없다", status_code=404)
    pending = await pending_review(graph, thread_id)
    if pending is None:
        # 이미 재개됐거나 승인 대기가 아닌 thread. 같은 결정이 두 번 와도 실행을 반복하지 않는다.
        result = snapshot.values.get(result_key)
        raise ResumeError(
            "NO_PENDING_APPROVAL",
            "대기 중인 심사가 없다" + (f" (이미 {result.get('status')})" if result else ""),
            status_code=409,
        )
    if pending.get("reviewKind") != review_kind:
        # 문맥 결정으로 강퇴를 승인하거나 그 반대가 되지 않게 한다.
        raise ResumeError("REVIEW_KIND_MISMATCH", "대기 중인 심사 종류가 다르다", status_code=409)
    if pending.get("caseId") != str(case_id):
        raise ResumeError("CASE_MISMATCH", "요청 사건과 대기 중인 사건이 다르다", status_code=409)
    if pending.get("reviewId") != str(review_id):
        raise ResumeError("REVIEW_MISMATCH", "대기 중인 심사와 reviewId가 다르다", status_code=409)

    final_state = await graph.ainvoke(
        Command(resume={"reviewId": str(review_id), "decisionId": str(decision_id)}),
        thread_config(thread_id),
    )
    return final_state.get(result_key, {})


async def resume_kick_review(
    graph, *, thread_id: str, review_id: UUID, decision_id: UUID, case_id: UUID
) -> dict[str, Any]:
    return await resume_review(
        graph, review_kind="KICK", thread_id=thread_id,
        review_id=review_id, decision_id=decision_id, case_id=case_id,
    )


async def resume_context_review(
    graph, *, thread_id: str, review_id: UUID, decision_id: UUID, case_id: UUID
) -> dict[str, Any]:
    return await resume_review(
        graph, review_kind="CONTEXT", thread_id=thread_id,
        review_id=review_id, decision_id=decision_id, case_id=case_id,
    )
