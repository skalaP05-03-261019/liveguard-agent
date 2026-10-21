"""Graph 공유 상태. 영속화·예산 저장 방식은 RAG/Harness 담당과 연결한다."""

from typing import Any, TypedDict

from app.schemas import JudgmentOutput, MessageFinding, TaskEnvelope


class AgentState(TypedDict, total=False):
    task: TaskEnvelope
    case_id: str
    task_id: str
    thread_id: str
    snapshot_id: str
    trace_id: str
    # 실행 한도·재시도 예산(#6). 재시작·재개 후에도 이어서 센다. task_outcome은 한도 초과·최종 실패 결과.
    harness_budget: dict[str, Any]
    task_outcome: str
    case_snapshot: dict[str, Any]
    evidence_version: int
    policy_version: str
    policy_evidence: list[dict[str, Any]]  # Verified full section + version + Resource URI; #8.
    messages: list[Any]
    target_messages: list[dict[str, Any]]
    context_messages: list[dict[str, Any]]
    message_findings: list[MessageFinding]
    evidence_refs: list[str]
    tool_observations: list[dict[str, Any]]  # 원문·토큰을 일반 trace에 복제하지 않는다.
    judgment: JudgmentOutput
    action_plan: dict[str, Any]  # TODO: Backend 합의 후 ActionCommand로 교체
    execution_results: list[dict[str, Any]]
    turn_count: int
    retry_count: int
    checkpoint_ref: str
    error: dict[str, Any]
    # 강퇴 승인 Harness(#5). kick_review는 Backend가 정한 심사 요청(required, reviewId, basisCaseIds)이다.
    kick_review: dict[str, Any]
    kick_review_id: str
    kick_decision_id: str
    basis_case_ids: list[str]
    kick_result: dict[str, Any]
    # 문맥 검토 Harness(#24). handling_route가 CONTEXT_REVIEW면 운영자 결정 전까지 멈춘다.
    # review_kind는 현재 대기·처리 중인 심사 종류(CONTEXT / KICK).
    handling_route: str
    review_kind: str
    context_review_id: str
    context_decision_id: str
    context_result: dict[str, Any]
    live_id: str
    user_id: str
    backend_result: dict[str, Any]
    investigation_error: dict[str, Any]
