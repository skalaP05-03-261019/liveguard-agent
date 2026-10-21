"""문맥 검토 Harness (POLICY 문맥 의심 경로, CONTRACTS §2·§5·§8·§11, #24).

문맥 의심 메시지(handling_route CONTEXT_REVIEW)는 AI 제안이 정상이어도 공개 명령이 아니다.
Backend에 검토 요청을 만들고(reviewId) 멈춘 뒤(interrupt), 운영자 결정으로 같은 thread를 재개한다.
결정 내용은 재개 메시지가 아니라 Backend 저장본으로 확인한다. 정상 확정 시 HELD 해제, 위반 확정 시
조치, 후속 강퇴 심사 연결은 #10이 context_result를 보고 처리한다.
"""

from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.harness.kick_review import _uuid, review_card
from app.state import AgentState

CONTEXT_DECISION_REQUIRED = "CONTEXT_DECISION_REQUIRED"
REVIEW_KIND = "CONTEXT"
CONTEXT_ROUTE = "CONTEXT_REVIEW"
CONFIRM_NORMAL = "CONFIRM_NORMAL"
CONFIRM_VIOLATION = "CONFIRM_VIOLATION"
DEFER = "DEFER"
CONTEXT_DECISIONS = (CONFIRM_NORMAL, CONFIRM_VIOLATION, DEFER)
# DEFER는 확정이 아니므로 같은 reviewId로 다시 기다린다. 그 밖의 결과는 이 thread의 문맥 검토를 끝낸다.
DEFERRED = "DEFERRED"


class ContextReviewGateway(Protocol):
    """Backend 문맥 검토 요청 생성과 결정 조회 경계. 실제 구현은 #10에서 연결한다."""

    async def create_review(self, *, card: dict[str, Any], trace_id: str) -> dict[str, Any]:
        """검토 요청 생성. caseId·threadId 기준 멱등이어야 한다(같은 요청은 같은 reviewId).

        {"reviewId": "uuid"}
        """
        ...

    async def get_decision(
        self, *, review_id: str, decision_id: str, trace_id: str
    ) -> dict[str, Any]:
        """업무 DB에 저장된 운영자 결정(POST /api/context-reviews/{id}/decision 결과).

        {"status": "DECIDED", "decision": "CONFIRM_NORMAL" | "CONFIRM_VIOLATION" | "DEFER",
         "categories": [...]} | {"status": "INVALID", "reason": "..."}
        """
        ...


def _finished(state: AgentState) -> bool:
    result = state.get("context_result") or {}
    return bool(result) and result.get("status") != DEFERRED


def context_card(state: AgentState, review_id: str | None = None) -> dict[str, Any]:
    """운영자 문맥 카드. AI 제안(findings)은 참고용이며 사람 결정 전 공개 여부를 정하지 않는다."""
    card = review_card(
        state, card_type=CONTEXT_DECISION_REQUIRED, review_kind=REVIEW_KIND, review_id=review_id
    )
    return {
        **card,
        # PII NEEDS_REVIEW uses the same CONTEXT review, without rewriting the
        # original case route (OpenAPI v1 A-16).
        "handlingRoute": state.get("handling_route", CONTEXT_ROUTE),
        "snapshotId": state.get("snapshot_id"),
        "evidenceVersion": state.get("evidence_version"),
        "allowedDecisions": list(CONTEXT_DECISIONS),
    }


async def request_context_review(state: AgentState, *, gateway: ContextReviewGateway) -> dict[str, Any]:
    """문맥 경로면 Backend에 검토 요청을 만든다. interrupt 노드와 분리해 재개 때 다시 호출되지 않게 한다."""
    if state.get("handling_route") != CONTEXT_ROUTE or state.get("context_review_id") or _finished(state):
        return {}
    created = await gateway.create_review(card=context_card(state), trace_id=state.get("trace_id", ""))
    review_id = _uuid(created.get("reviewId")) if isinstance(created, dict) else None
    if review_id is None:
        return {"context_result": {"status": "INVALID_REVIEW_REQUEST"}}
    return {"review_kind": REVIEW_KIND, "context_review_id": review_id}


async def review_context(state: AgentState) -> dict[str, Any]:
    """운영자 결정(decisionId)을 기다린다. AI가 정상으로 제안해도 반드시 멈춘다."""
    review_id = state.get("context_review_id")
    if not review_id or _finished(state):
        return {}
    # 재개 시 이 노드는 처음부터 다시 실행되고 interrupt()가 운영자 결정을 돌려준다.
    decision = interrupt(context_card(state, review_id))
    decision_id = _uuid(decision.get("decisionId")) if isinstance(decision, dict) else None
    if decision_id is None or decision.get("reviewId") != review_id:
        return {"context_result": {"status": "INVALID_DECISION", "reviewId": review_id}}
    return {"review_kind": REVIEW_KIND, "context_decision_id": decision_id}


async def apply_context_decision(state: AgentState, *, gateway: ContextReviewGateway) -> dict[str, Any]:
    """저장된 결정을 확인해 context_result에 남긴다. 모델 결과(message_findings)는 바꾸지 않는다."""
    decision_id = state.get("context_decision_id")
    if not decision_id or _finished(state):
        return {}
    review_id = state["context_review_id"]
    refs = {"reviewId": review_id, "decisionId": decision_id}
    if (state.get("context_result") or {}).get("decisionId") == decision_id:
        return {}  # 이미 반영한 보류 결정. 다음 결정을 기다린다.
    stored = await gateway.get_decision(
        review_id=review_id, decision_id=decision_id, trace_id=state.get("trace_id", "")
    )
    decision = stored.get("decision")
    if stored.get("status") != "DECIDED" or decision not in CONTEXT_DECISIONS:
        return {"context_result": {"status": "DECISION_NOT_VERIFIED", **refs}}
    if decision == DEFER:
        # 보류는 공개 승인이 아니다. HELD를 유지하고 같은 reviewId로 다시 기다린다.
        return {"context_result": {"status": DEFERRED, "decision": DEFER, **refs}}
    result = {"status": "DECIDED", "decision": decision, **refs}
    if decision == CONFIRM_VIOLATION:
        # 위반 유형·증거 검증은 Backend가 결정 저장 시 한다. 여기서는 저장본을 그대로 전달만 한다.
        result["categories"] = list(stored.get("categories") or [])
    return {"context_result": result}


def route_context_decision(state: AgentState) -> str:
    """보류면 다시 대기, 그 밖에는 다음 단계(#10의 Backend 처리)로."""
    if (state.get("context_result") or {}).get("status") == DEFERRED:
        return "review_context"
    return "done"


def build_context_review_graph(gateway: ContextReviewGateway, *, checkpointer=None):
    """문맥 검토 단계만 실행하는 Graph. 테스트·증거용이며 전체 Graph 연결은 #10에서 같은 노드를 쓴다."""

    async def request_node(state: AgentState) -> dict[str, Any]:
        return await request_context_review(state, gateway=gateway)

    async def apply_node(state: AgentState) -> dict[str, Any]:
        return await apply_context_decision(state, gateway=gateway)

    graph = StateGraph(AgentState)
    graph.add_node("request_context_review", request_node)
    graph.add_node("review_context", review_context)
    graph.add_node("apply_context_decision", apply_node)
    graph.add_edge(START, "request_context_review")
    graph.add_edge("request_context_review", "review_context")
    graph.add_edge("review_context", "apply_context_decision")
    graph.add_conditional_edges(
        "apply_context_decision", route_context_decision,
        {"review_context": "review_context", "done": END},
    )
    return graph.compile(checkpointer=checkpointer)
