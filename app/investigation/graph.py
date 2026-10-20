"""Standalone investigation subgraph; no edits to AI2's top-level approval Graph."""

from langgraph.graph import END, START, StateGraph

from app import nodes
from app.state import AgentState


def build_investigation_graph(*, model, business, policy, gate=None, present_data=None):
    async def investigate(state):
        return await nodes.investigate(state, model=model, business=business, policy=policy,
                                       gate=gate, present_data=present_data)

    async def load_case(state):
        return await nodes.load_case(state, tools=business)

    graph = StateGraph(AgentState)
    graph.add_node("load_case", load_case)
    graph.add_node("investigate", investigate)
    graph.add_node("validate_judgment", nodes.validate_judgment)
    graph.add_edge(START, "load_case")
    graph.add_edge("load_case", "investigate")
    graph.add_edge("investigate", "validate_judgment")
    graph.add_edge("validate_judgment", END)
    return graph.compile()
