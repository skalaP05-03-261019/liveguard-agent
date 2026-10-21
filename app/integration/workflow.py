"""Connect #9 investigation to AI2 #24 CONTEXT and #5 KICK nodes.

No inferred Backend URL, fabricated evidence version, model-issued command or
implicit task acceptance. A real gateway and persistent checkpointer are required.
"""

from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app import nodes
from app.case_loader import require_executable_case
from app.clients.business_mcp import AgentToolError
from app.business_mcp.schemas import KickCommand
from app.harness.context_review import (
    apply_context_decision, request_context_review, review_context,
)
from app.harness.execution import guarded_investigate
from app.harness.kick_review import execute_kick, review_kick
from app.harness.kick_review import _uuid
from app.harness.safety import safe_presentation
from app.schemas import AnalysisResult
from app.state import AgentState


class ModerationGateway(Protocol):
    """BE-owned operations. Exact HTTP routes/JSON require B1/B2 agreement.

    submit_analysis is only for the separate PII route. apply_context acts on the
    persisted human decision, not on AI findings. Both are idempotent by task /
    review+decision. Return case/live/snapshot/evidence/policy refs plus kickReview.
    State fields and model verdict are never treated as operator authorization.
    """

    async def create_review(self, *, card: dict, trace_id: str) -> dict: ...
    async def get_decision(self, *, review_id: str, decision_id: str, trace_id: str) -> dict: ...
    async def submit_analysis(self, *, analysis: dict, trace_id: str) -> dict: ...
    async def apply_context(self, *, request: dict, trace_id: str) -> dict: ...
    async def revalidate(self, *, review_id: str, decision_id: str, trace_id: str) -> dict: ...
    async def kick(self, action_command: dict, *, trace_id: str) -> dict: ...


def _error(state, code):
    return {"task_outcome": "FAILED", "error": {
        "code": code, "retryable": False, "traceId": state.get("trace_id", "")}}


def _refs(state):
    return {"caseId": state["case_id"], "liveId": state["live_id"],
            "snapshotId": state["snapshot_id"], "evidenceVersion": state["evidence_version"],
            "policyVersion": state["policy_version"]}


def _presented_state(state):
    shown = dict(state)
    for key in ("target_messages", "context_messages", "message_findings"):
        values = state.get(key, [])
        shown[key] = safe_presentation([
            value.model_dump(mode="json", by_alias=True) if hasattr(value, "model_dump") else value
            for value in values])
    return shown


def _result_patch(state, result):
    # A successful HTTP response is not proof that the intended case was applied.
    if not isinstance(result, dict) or any(result.get(k) != v for k, v in _refs(state).items()):
        return _error(state, "INVALID_BACKEND_RESULT")
    if result.get("observation") == "UNKNOWN":
        return {"backend_result": result, "task_outcome": "UNKNOWN"}
    if result.get("observation") != "CONFIRMED":
        return _error(state, "INVALID_BACKEND_RESULT")
    if result.get("status") not in {"APPLIED", "NO_ACTION", "NEEDS_REVIEW"}:
        return {"backend_result": result, **_error(state, "BACKEND_NOT_APPLIED")}
    review = result.get("kickReview", {"required": False})
    if not isinstance(review, dict) or type(review.get("required")) is not bool:
        return _error(state, "INVALID_BACKEND_RESULT")
    if review["required"] and result.get("status") != "APPLIED":
        return _error(state, "INVALID_BACKEND_RESULT")
    return {"backend_result": result, "kick_review": review}


def build_moderation_graph(*, model, business, policy, backend: ModerationGateway,
                           checkpointer, observer=None):
    """Integrated Graph; the HTTP /run task dispatcher is a separate boundary.

    backend is trusted service code, never the model. It must validate stored
    decisions and refs at the DB boundary. Read-only fixture success is not live
    Backend integration proof. Use one thread for CONTEXT then KICK interrupts.
    """
    if checkpointer is None:
        raise ValueError("Persistent checkpointer must be configured by the task runner")

    async def load(state):
        task = state["task"]
        if state.get("case_id") or state.get("task_outcome"):
            return _error(state, "TASK_STATE_REUSE")
        if task.kind not in {"ANALYZE", "REANALYZE"}:
            return _error(state, "INVALID_TASK_KIND")
        try:
            loaded = await nodes.load_case(state, tools=business)
            require_executable_case(loaded)
        except AgentToolError as error:
            patch = _error({"trace_id": str(task.trace_id)}, error.code)
            patch["error"]["retryable"] = error.retryable
            return patch
        return loaded

    def after_load(state):
        if state.get("task_outcome"):
            return "finish"
        route = state["handling_route"]
        if route == "RULE_CONFIRMED":
            return "review_kick"
        if route == "PASS":
            return "finish"
        if route == "FLOOD_REVIEW":
            return "flood_pending"
        return "investigate"

    async def investigate(state):
        import time
        async def progress(event, payload):
            publish = getattr(backend,"progress",None)
            if publish:
                try:
                    await publish(case_id=state["case_id"],trace_id=state["trace_id"],event=event,payload=payload)
                except AgentToolError:
                    pass  # A status notification cannot authorize or repeat a mutation.
        started=time.monotonic()
        await progress("TOOL_START",{"tool":"investigate","callId":state["task_id"]})
        result = await guarded_investigate(state, model=model, business=business,
                                           policy=policy, observer=observer)
        await progress("TOOL_END",{"tool":"investigate","callId":state["task_id"],
                       "status":result.get("task_outcome") or "COMPLETED",
                       "elapsedMs":int((time.monotonic()-started)*1000)})
        return result

    def after_investigation(state):
        if (state.get("task_outcome") == "NEEDS_REVIEW"
                and (state.get("error") or {}).get("code") == "EXECUTION_LIMIT"):
            return "manual_review"
        if state.get("task_outcome"):
            return "finish"
        return "validate"

    async def manual_review(state):
        # A bounded investigation may stop without a verdict. Keep HELD and
        # expose the limitation to the operator; do not invent policy evidence.
        from app.schemas import MessageFinding
        return {"task_outcome": None, "error": None,
                "investigation_error": state.get("error"),
                "message_findings": [MessageFinding.model_validate({
                    "messageId": message["messageId"], "verdict": "NEEDS_REVIEW",
                    "categories": [], "policyIds": [],
                    "evidenceMessageIds": [message["messageId"]],
                    "reason": "AI 조사 실행 한도에 도달해 판단을 완료하지 못했습니다.",
                    "missingFacts": ["정책과 문맥을 운영자가 직접 확인해야 합니다. (EXECUTION_LIMIT)"]})
                    for message in state["target_messages"]]}

    async def validate(state):
        try:
            return await nodes.validate_judgment(state)
        except AgentToolError as error:
            return _error(state, error.code)

    def after_validation(state):
        if state.get("task_outcome"):
            return "finish"
        return "request_context_review" if state["handling_route"] == "CONTEXT_REVIEW" else "submit_pii"

    async def request_review(state):
        # AI2 cards must contain masked text, with explicit live scope from BE.
        shown = _presented_state(state)
        class ScopedGateway:
            async def create_review(self, *, card, trace_id):
                return await backend.create_review(card={**card, "liveId": state["live_id"]}, trace_id=trace_id)
        return await request_context_review(shown, gateway=ScopedGateway())

    async def context_wait(state):
        return await review_context(_presented_state(state))

    async def kick_wait(state):
        return await review_kick(_presented_state(state))

    async def decide(state):
        class BoundDecision:
            async def get_decision(self, *, review_id, decision_id, trace_id):
                stored = await backend.get_decision(review_id=review_id, decision_id=decision_id, trace_id=trace_id)
                refs = {**_refs(state), "reviewId": review_id, "decisionId": decision_id,
                        "threadId": state["thread_id"]}
                if not isinstance(stored, dict) or any(stored.get(k) != v for k, v in refs.items()):
                    return {"status": "INVALID"}
                return stored
        return await apply_context_decision(state, gateway=BoundDecision())

    def after_decision(state):
        result = state.get("context_result", {})
        if result.get("status") == "DEFERRED":
            return "review_context"
        if result.get("status") == "DECIDED":
            return "apply_context"
        return "finish"

    async def apply_human_decision(state):
        # Do not overwrite message_findings with the human verdict; BE reads the decision.
        request = {**_refs(state), "taskId": state["task_id"], "threadId": state["thread_id"],
                   "reviewId": state["context_review_id"], "decisionId": state["context_decision_id"]}
        result = await backend.apply_context(request=request, trace_id=state["trace_id"])
        return _result_patch(state, result)

    async def submit_pii(state):
        analysis = AnalysisResult.model_validate({
            **{k: v for k, v in _refs(state).items() if k != "liveId"},
            "messageFindings": [f.model_dump(mode="json", by_alias=True) for f in state["message_findings"]]})
        payload = {**analysis.model_dump(mode="json", by_alias=True), "liveId": state["live_id"],
                   "handlingRoute": state["handling_route"], "taskId": state["task_id"]}
        result = await backend.submit_analysis(analysis=payload, trace_id=state["trace_id"])
        if any(f.verdict == "NEEDS_REVIEW" for f in analysis.message_findings):
            # OpenAPI v1 A-16: judgment itself creates the CONTEXT review. Do not
            # submit the same analysis through create_review or invent a review ID.
            review_id = _uuid(result.get("reviewId")) if isinstance(result, dict) else None
            if (review_id is None or result.get("reviewKind") != "CONTEXT"
                    or result.get("confirmationStatus") != "PENDING"
                    or any(k in result and result[k] != v for k, v in _refs(state).items())):
                return _error(state, "INVALID_BACKEND_RESULT")
            return {"backend_result": result, "review_kind": "CONTEXT",
                    "context_review_id": review_id}
        return _result_patch(state, result)

    def after_pii(state):
        if state.get("task_outcome"):
            return "finish"
        return "review_context" if state.get("context_review_id") else "review_kick"

    def after_backend(state):
        return "finish" if state.get("task_outcome") else "review_kick"

    async def kick(state):
        # Existing MCP retry owner remains unchanged; no outer mutation retry loop.
        class BoundKick:
            async def revalidate(self, *, review_id, decision_id, trace_id):
                result = await backend.revalidate(review_id=review_id, decision_id=decision_id, trace_id=trace_id)
                refs = {**_refs(state), "reviewId": review_id, "decisionId": decision_id}
                if not isinstance(result, dict) or any(result.get(k) != v for k, v in refs.items()):
                    return {"status": "INVALID"}
                command = result.get("actionCommand")
                if result.get("status") == "APPROVED":
                    try:
                        validated = KickCommand.model_validate(command)
                    except ValidationError:
                        return {"status": "INVALID"}
                    # Live/review refs are bound by revalidation above. Keep the existing
                    # MCP command shape; injecting new fields would make MCP reject it.
                    if (str(validated.case_id) != state["case_id"]
                            or str(validated.target_id) != state["user_id"]
                            or str(validated.decision_id) != decision_id
                            or validated.policy_version != state["policy_version"]
                            or validated.expected_evidence_version != state["evidence_version"]):
                        return {"status": "INVALID"}
                    if validated.live_id is not None and (str(validated.live_id) != state["live_id"]
                            or str(validated.review_id) != review_id):
                        return {"status":"INVALID"}
                return result

            async def kick(self, action_command, *, trace_id):
                return await backend.kick(action_command, trace_id=trace_id)
        return await execute_kick(state, gateway=BoundKick())

    async def finish(state):
        if state.get("task_outcome"):
            return {}
        context = state.get("context_result", {})
        kick_result = state.get("kick_result", {})
        if kick_result.get("status") == "UNKNOWN":
            return {"task_outcome": "UNKNOWN"}
        if (context and context.get("status") != "DECIDED") or (kick_result and kick_result.get("status") not in {
            "APPLIED", "SUCCEEDED", "REJECTED"}):
            return _error(state, "REVIEW_NOT_COMPLETED")
        return {"task_outcome": "NEEDS_REVIEW" if state.get("backend_result", {}).get("status") == "NEEDS_REVIEW"
                else "SUCCEEDED"}

    async def flood_pending(state):
        # TBD-11 has no agreed first confirmation owner. Do not manufacture a rule.
        return {"task_outcome": "NEEDS_REVIEW", "error": {"code": "FLOOD_CONFIRMATION_UNDECIDED",
                "retryable": False, "traceId": state["trace_id"]}}

    graph = StateGraph(AgentState)
    for name, fn in {"load_case": load, "investigate": investigate, "validate": validate,
                     "request_context_review": request_review, "review_context": context_wait,
                     "apply_context_decision": decide, "apply_context": apply_human_decision,
                     "submit_pii": submit_pii, "review_kick": kick_wait, "execute_kick": kick,
                     "flood_pending": flood_pending, "finish": finish}.items():
        graph.add_node(name, fn)
    graph.add_node("manual_review", manual_review)
    graph.add_edge("manual_review", "validate")
    graph.add_edge(START, "load_case")
    for source, router, destinations in [
        ("load_case", after_load, ["finish", "review_kick", "flood_pending", "investigate"]),
        ("investigate", after_investigation, ["finish", "validate", "manual_review"]),
        ("validate", after_validation, ["finish", "request_context_review", "submit_pii"]),
        ("apply_context_decision", after_decision, ["review_context", "apply_context", "finish"]),
        ("apply_context", after_backend, ["finish", "review_kick"]),
        ("submit_pii", after_pii, ["finish", "review_context", "review_kick"]),
    ]:
        graph.add_conditional_edges(source, router, {name: name for name in destinations})
    graph.add_edge("request_context_review", "review_context")
    graph.add_edge("review_context", "apply_context_decision")
    graph.add_edge("review_kick", "execute_kick")
    graph.add_edge("execute_kick", "finish")
    graph.add_edge("flood_pending", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)
