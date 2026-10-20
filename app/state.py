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
