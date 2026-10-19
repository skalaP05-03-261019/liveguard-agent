"""Graph 공유 상태. 영속화·예산 저장 방식은 RAG/Harness 담당과 연결한다."""

from typing import Any, TypedDict

from app.schemas import JudgmentOutput, TaskEnvelope


class AgentState(TypedDict, total=False):
    task: TaskEnvelope
    case_snapshot: dict[str, Any]
    case_version: int
    evidence_version: int
    policy_version: str
    messages: list[Any]
    evidence_refs: list[str]
    tool_observations: list[dict[str, Any]]  # 원문·토큰을 일반 trace에 복제하지 않는다.
    judgment: JudgmentOutput
    action_plan: dict[str, Any]  # TODO: Backend 합의 후 ActionCommand로 교체
    execution_results: list[dict[str, Any]]
    turn_count: int
    retry_count: int
    checkpoint_ref: str
    error: dict[str, Any]
