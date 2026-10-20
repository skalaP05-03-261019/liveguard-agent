"""강퇴 승인 Harness (POLICY §8~10, CONTRACTS §6·§9·§11, 운영정책 MP-4.1).

통합 사건 집계·심사 필요 여부·기준 사건(basisCaseIds)·예약·소진은 Backend가 정해
State의 `kick_review`에 넣는다. 이 모듈은 요청이 있으면 실행 전에 멈추고(interrupt),
운영자 결정으로 같은 thread를 재개해 Backend 재검증 후 승인일 때만 kick_user를 호출한다.
"""

from typing import Any, Protocol
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.state import AgentState

KICK_APPROVAL_REQUIRED = "KICK_APPROVAL_REQUIRED"
REVIEW_KIND = "KICK"
MIN_BASIS_CASES = 3


class KickGateway(Protocol):
    """Backend 재검증과 Business MCP kick_user 호출 경계. 실제 구현은 #10에서 연결한다."""

    async def revalidate(
        self, *, review_id: str, decision_id: str, trace_id: str
    ) -> dict[str, Any]:
        """업무 DB에 저장된 결정과 원 승인 기준을 재검증한 결과
        (POST /internal/kick-reviews/{id}/revalidate).

        {"status": "APPROVED", "actionCommand": {...}} | {"status": "REJECTED"} |
        {"status": "INVALID", "reason": "..."}
        """
        ...

    async def kick(self, action_command: dict[str, Any], *, trace_id: str) -> dict[str, Any]:
        """Backend가 발급한 KICK 명령으로 kick_user를 호출한 결과 data."""
        ...


def _uuid(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


def _basis_case_ids(kick_review: dict[str, Any]) -> list[str] | None:
    raw = kick_review.get("basisCaseIds")
    if not isinstance(raw, list):
        return None
    ids = [_uuid(value) for value in raw]
    # 같은 사건이 두 번 들어와도 2건으로 세지 않는다(Backend 집계 결과의 방어적 재확인).
    if None in ids or len(ids) != len(set(ids)) or len(ids) < MIN_BASIS_CASES:
        return None
    return ids


def approval_payload(state: AgentState, review_id: str, basis_case_ids: list[str]) -> dict[str, Any]:
    """운영자 강퇴 카드용. 마스킹된 메시지와 판단 근거만 담고 기간·실행 명령은 넣지 않는다."""
    findings = [
        finding.model_dump(mode="json", by_alias=True) if hasattr(finding, "model_dump") else finding
        for finding in state.get("message_findings", [])
    ]
    return {
        "type": KICK_APPROVAL_REQUIRED,
        "reviewKind": REVIEW_KIND,
        "reviewId": review_id,
        "caseId": state.get("case_id"),
        "taskId": state.get("task_id"),
        "threadId": state.get("thread_id"),
        "traceId": state.get("trace_id"),
        "policyVersion": state.get("policy_version"),
        "basisCaseIds": basis_case_ids,
        "targetMessages": state.get("target_messages", []),
        "contextMessages": state.get("context_messages", []),
        "findings": [
            {key: finding.get(key) for key in ("messageId", "verdict", "categories", "policyIds", "reason")}
            for finding in findings
        ],
        "policyEvidence": [
            {key: item.get(key) for key in ("policyId", "title", "resourceUri")}
            for item in state.get("policy_evidence", [])
        ],
    }


async def review_kick(state: AgentState) -> dict[str, Any]:
    """Backend가 강퇴 심사를 요청했으면 멈추고 운영자 결정(decisionId)을 기다린다."""
    kick_review = state.get("kick_review") or {}
    if not kick_review.get("required") or state.get("kick_result"):
        return {}
    review_id = _uuid(kick_review.get("reviewId"))
    basis_case_ids = _basis_case_ids(kick_review)
    if review_id is None or basis_case_ids is None:
        return {"kick_result": {"status": "INVALID_REVIEW_REQUEST", "kicked": False}}

    # 재개 시 이 노드는 처음부터 다시 실행되고 interrupt()가 운영자 결정을 돌려준다.
    # 그래서 interrupt 앞에는 부수 효과를 두지 않는다.
    decision = interrupt(approval_payload(state, review_id, basis_case_ids))
    decision_id = _uuid(decision.get("decisionId")) if isinstance(decision, dict) else None
    if decision_id is None or decision.get("reviewId") != review_id:
        return {"kick_result": {"status": "INVALID_DECISION", "kicked": False, "reviewId": review_id}}
    return {
        "kick_review_id": review_id,
        "kick_decision_id": decision_id,
        "basis_case_ids": basis_case_ids,
    }


async def execute_kick(state: AgentState, *, gateway: KickGateway) -> dict[str, Any]:
    """Backend 재검증 후 승인이면 kick_user를 호출한다. 거절이면 강퇴하지 않는다."""
    decision_id = state.get("kick_decision_id")
    if not decision_id or state.get("kick_result"):
        return {}
    trace_id = state.get("trace_id", "")
    refs = {"reviewId": state["kick_review_id"], "decisionId": decision_id}
    # 승인 여부는 재개 메시지가 아니라 업무 DB 저장본으로 확인한다(CONTRACTS §11).
    verified = await gateway.revalidate(
        review_id=state["kick_review_id"], decision_id=decision_id, trace_id=trace_id
    )
    status = verified.get("status")
    if status == "REJECTED":
        # 기존 HIDE·TIMEOUT은 유지한다. 기준 사건 소진은 Backend가 결정 저장 시 기록한다.
        return {"kick_result": {"status": "REJECTED", "kicked": False, **refs}}
    if status != "APPROVED" or not isinstance(verified.get("actionCommand"), dict):
        # 취소·시간창 이탈·타 심사 소진 등으로 재검증 미통과. 후속 처리는 TBD-06.
        return {"kick_result": {"status": "REVALIDATION_FAILED", "kicked": False, **refs}}

    # 같은 actionId·idempotencyKey로만 호출한다. kick_user 일시 오류 재시도는 Business MCP가 한다.
    result = await gateway.kick(verified["actionCommand"], trace_id=trace_id)
    kick_status = result.get("status", "UNKNOWN")
    return {
        "kick_result": {
            "status": kick_status,
            # 응답이 없거나 미확인이면 성공·실패로 단정하지 않는다(UNKNOWN).
            "kicked": True if kick_status in {"APPLIED", "SUCCEEDED"} else (
                None if kick_status == "UNKNOWN" else False
            ),
            "actionId": result.get("actionId"),
            **refs,
        }
    }


def build_kick_review_graph(gateway: KickGateway, *, checkpointer=None):
    """강퇴 승인 단계만 실행하는 Graph. 테스트·증거용이며 전체 Graph는 같은 노드를 쓴다."""

    async def execute_kick_node(state: AgentState) -> dict[str, Any]:
        return await execute_kick(state, gateway=gateway)

    graph = StateGraph(AgentState)
    graph.add_node("review_kick", review_kick)
    graph.add_node("execute_kick", execute_kick_node)
    graph.add_edge(START, "review_kick")
    graph.add_edge("review_kick", "execute_kick")
    graph.add_edge("execute_kick", END)
    return graph.compile(checkpointer=checkpointer)
