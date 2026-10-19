"""v0.3 초기 분석 Graph 골격. 실제 호출·재개·복구 구현은 후속 작업이다."""

from langgraph.graph import END, START, StateGraph

from app import nodes
from app.schemas import Judgment
from app.state import AgentState


def route_judgment(state: AgentState) -> str:
    output = state["judgment"]
    if output.judgment == Judgment.NEEDS_REVIEW:
        return "wait_review"
    if output.requested_actions:
        return "request_plan"
    return "report_result"


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState)
    for name in (
        "load_case", "investigate", "validate_judgment", "request_plan",
        "execute_plan", "verify_result", "wait_review", "report_result",
    ):
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "load_case")
    graph.add_edge("load_case", "investigate")
    graph.add_edge("investigate", "validate_judgment")
    graph.add_conditional_edges("validate_judgment", route_judgment, {
        "wait_review": "wait_review",
        "request_plan": "request_plan",
        "report_result": "report_result",
    })
    graph.add_edge("request_plan", "execute_plan")
    graph.add_edge("execute_plan", "verify_result")
    graph.add_edge("verify_result", "report_result")
    # TODO: wait_review는 현재 실행 불가. 결정 재검증·재개 분기는 Harness 합의 후 추가.
    graph.add_edge("wait_review", END)
    graph.add_edge("report_result", END)
    return graph.compile(checkpointer=checkpointer)
