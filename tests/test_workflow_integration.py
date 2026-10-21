"""Graph integration with controlled BE/model/RAG; not actual Spring mutation proof."""

from copy import deepcopy
import json
import os
import unittest
from uuid import UUID, uuid4

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from app.case_loader import load_case_data
from app.clients.business_mcp import AgentToolError
from app.harness.resume import ResumeError, pending_review, resume_review
from app.harness.checkpoint import open_checkpointer, state_serializer
from app.integration.backend_events import BackendEvents
from app.integration.workflow import build_moderation_graph
from app.schemas import TaskEnvelope
from test_agent_read import FIXTURE, FixtureTools
from test_investigation import Policies, ScriptedModel, finding, finish, search


LIVE = "00000000-0000-4000-8000-000000000011"
USER = "00000000-0000-4000-8000-000000000012"
REVIEW = "00000000-0000-4000-8000-000000000013"
DECISION = "00000000-0000-4000-8000-000000000014"
KICK = "00000000-0000-4000-8000-000000000015"


class Backend:
    def __init__(self, state):
        self.state, self.calls = state, []
        self.decision = "CONFIRM_NORMAL"
        self.wrong_live = False
        self.status = "APPLIED"
        self.require_kick = False

    def refs(self):
        return {"caseId": self.state["case_id"], "liveId": LIVE,
                "snapshotId": self.state["snapshot_id"], "evidenceVersion": 1,
                "policyVersion": self.state["policy_version"]}

    async def create_review(self, *, card, trace_id):
        self.calls.append(("create", deepcopy(card)))
        return {"reviewId": REVIEW}

    async def get_decision(self, *, review_id, decision_id, trace_id):
        self.calls.append(("decision", decision_id))
        return {**self.refs(), "liveId": USER if self.wrong_live else LIVE,
                "threadId": self.state["thread_id"], "reviewId": review_id, "decisionId": decision_id,
                "status": "DECIDED", "decision": self.decision, "categories": ["HARASSMENT"]}

    def result(self):
        review = {"required": False}
        if self.require_kick:
            review = {"required": True, "reviewId": KICK, "basisCaseIds": [REVIEW, DECISION, self.state["case_id"]]}
        return {**self.refs(), "status": self.status, "observation": "CONFIRMED", "kickReview": review}

    async def apply_context(self, *, request, trace_id):
        self.calls.append(("apply", deepcopy(request)))
        return self.result()

    async def submit_analysis(self, *, analysis, trace_id):
        self.calls.append(("submit", deepcopy(analysis)))
        if any(f["verdict"] == "NEEDS_REVIEW" for f in analysis["messageFindings"]):
            return {"confirmationStatus": "PENDING", "reviewId": REVIEW, "reviewKind": "CONTEXT"}
        return self.result()

    async def revalidate(self, **kwargs):
        self.calls.append(("revalidate", kwargs))
        return {**self.refs(), "reviewId": kwargs["review_id"], "decisionId": kwargs["decision_id"],
                "status": "REJECTED"}

    async def kick(self, *args, **kwargs):
        self.calls.append(("kick", args))
        raise AssertionError("Rejection must not kick")


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, route="CONTEXT_REVIEW", verdict="NO_VIOLATION", **extra):
        fixture = deepcopy(FIXTURE)
        fixture["case"].update(liveId=LIVE, userId=USER, handlingRoute=route, **extra)
        for message in fixture["context"]["messages"]:
            message["visibility"] = "HELD"
        self.business = FixtureTools(fixture)
        self.task = TaskEnvelope.model_validate(fixture["task"])
        self.state = await load_case_data(self.task, self.business)
        self.backend = Backend(self.state)
        self.model = ScriptedModel([search(), finish([finding(self.state, verdict)])])
        self.graph = build_moderation_graph(model=self.model, business=self.business, policy=Policies(),
                                            backend=self.backend, checkpointer=InMemorySaver(serde=state_serializer()))
        self.config = {"configurable": {"thread_id": self.task.thread_id}}

    async def start(self):
        return await self.graph.ainvoke({"task": self.task}, self.config)

    async def resume(self, kind="CONTEXT", review=REVIEW, decision=DECISION):
        return await resume_review(self.graph, review_kind=kind, thread_id=self.task.thread_id,
            review_id=UUID(review), decision_id=UUID(decision), case_id=self.task.case_id)

    async def test_backend_confirmed_flood_routes_to_separate_kick_review_without_model(self):
        await self.prepare(route="FLOOD_REVIEW",confirmationStatus="CONFIRMED_VIOLATION",
                           kickReview={"required":True,"reviewId":KICK,"basisCaseIds":[REVIEW,DECISION,str(uuid4())]})
        await self.start()
        self.assertEqual((await pending_review(self.graph,self.task.thread_id))["reviewId"],KICK)
        self.assertEqual(self.model.inputs,[])
        self.assertEqual(self.backend.calls,[])

    async def test_all_model_verdicts_wait_for_context_decision_without_mutation(self):
        for verdict in ("NO_VIOLATION", "VIOLATION", "NEEDS_REVIEW"):
            await self.prepare(verdict=verdict)
            result = await self.start()
            pending = await pending_review(self.graph, self.task.thread_id)
            self.assertEqual(pending["reviewKind"], "CONTEXT")
            self.assertEqual(pending["liveId"], LIVE)
            self.assertEqual([c[0] for c in self.backend.calls], ["create"])
            self.assertNotIn("task_outcome", result)
            self.assertEqual(result["message_findings"][0].verdict, verdict)
            self.assertGreater(result["harness_budget"]["modelCalls"], 0)

    async def test_normal_human_decision_applied_once_and_model_proposal_preserved(self):
        await self.prepare(verdict="VIOLATION")
        await self.start()
        await self.resume()
        state = (await self.graph.aget_state(self.config)).values
        self.assertEqual(state["task_outcome"], "SUCCEEDED")
        self.assertEqual(state["message_findings"][0].verdict, "VIOLATION")
        self.assertEqual(state["context_result"]["decision"], "CONFIRM_NORMAL")
        request = next(c[1] for c in self.backend.calls if c[0] == "apply")
        self.assertNotIn("durationSeconds", request)
        self.assertEqual(request["liveId"], LIVE)
        with self.assertRaises(ResumeError):
            await self.resume()
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_defer_rewaits_same_review_no_reinvestigation_then_normal(self):
        await self.prepare()
        await self.start()
        calls = len(self.model.inputs)
        self.backend.decision = "DEFER"
        await self.resume()
        self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewId"], REVIEW)
        self.assertNotIn("apply", [c[0] for c in self.backend.calls])
        self.backend.decision = "CONFIRM_NORMAL"
        await self.resume(decision=USER)
        self.assertEqual(len(self.model.inputs), calls)
        self.assertEqual(sum(c[0] == "create" for c in self.backend.calls), 1)

    async def test_violation_context_then_separate_kick_approval_and_rejection(self):
        await self.prepare()
        self.backend.decision = "CONFIRM_VIOLATION"
        self.backend.require_kick = True
        await self.start()
        with self.assertRaises(ResumeError) as raised:
            await self.resume(kind="KICK")
        self.assertEqual(raised.exception.code, "REVIEW_KIND_MISMATCH")
        await self.resume()
        pending = await pending_review(self.graph, self.task.thread_id)
        self.assertEqual(pending["reviewKind"], "KICK")
        self.assertNotIn("kick", [c[0] for c in self.backend.calls])
        await self.resume(kind="KICK", review=KICK, decision=USER)
        state = (await self.graph.aget_state(self.config)).values
        self.assertEqual(state["kick_result"]["status"], "REJECTED")
        self.assertNotIn("kick", [c[0] for c in self.backend.calls])

    async def test_wrong_live_decision_fails_without_backend_apply(self):
        await self.prepare()
        await self.start()
        self.backend.wrong_live = True
        await self.resume()
        state = (await self.graph.aget_state(self.config)).values
        self.assertEqual(state["task_outcome"], "FAILED")
        self.assertNotIn("apply", [c[0] for c in self.backend.calls])

    async def test_pii_needs_review_waits_on_backend_review_without_second_creation(self):
        await self.prepare(route="PII_INVESTIGATION", verdict="NEEDS_REVIEW")
        state = await self.start()
        self.assertEqual([c[0] for c in self.backend.calls], ["submit"])
        self.assertNotIn("task_outcome", state)
        pending = await pending_review(self.graph, self.task.thread_id)
        self.assertEqual(pending["reviewId"], REVIEW)
        self.assertEqual(pending["reviewKind"], "CONTEXT")
        self.assertEqual(pending["handlingRoute"], "PII_INVESTIGATION")
        await self.resume()
        final = (await self.graph.aget_state(self.config)).values
        self.assertEqual(final["task_outcome"], "SUCCEEDED")
        self.assertEqual([c[0] for c in self.backend.calls], ["submit", "decision", "apply"])

    async def test_pii_normal_and_violation_do_not_gain_mandatory_approval(self):
        for verdict in ("NO_VIOLATION", "VIOLATION"):
            await self.prepare(route="PII_INVESTIGATION", verdict=verdict)
            state = await self.start()
            self.assertEqual(state["task_outcome"], "SUCCEEDED")
            self.assertEqual([c[0] for c in self.backend.calls], ["submit"])
            self.assertIsNone(await pending_review(self.graph, self.task.thread_id))

    async def test_pii_one_uncertain_finding_in_mixed_targets_waits_for_context(self):
        await self.prepare(route="PII_INVESTIGATION")
        second = self.business.fixture["context"]["messages"][1]
        second["authorAlias"] = "U1"
        second_id = second["messageId"]
        self.business.fixture["case"]["targetMessageIds"].append(second_id)
        self.business.fixture["case"]["targetVersions"][second_id] = 1
        normal = finding(self.state, "NO_VIOLATION")
        uncertain = {**finding(self.state, "NEEDS_REVIEW"), "messageId": second_id,
                     "evidenceMessageIds": [second_id]}
        self.model.calls = [search(), finish([normal, uncertain])]
        state = await self.start()
        self.assertNotIn("task_outcome", state)
        self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewId"], REVIEW)
        self.assertEqual([c[0] for c in self.backend.calls], ["submit"])

    async def test_pii_needs_review_rejects_invalid_backend_review(self):
        for result in ({"confirmationStatus": "PENDING", "reviewId": REVIEW, "reviewKind": "KICK"},
                       {"confirmationStatus": "CONFIRMED_NORMAL", "reviewId": REVIEW, "reviewKind": "CONTEXT"},
                       {"confirmationStatus": "PENDING", "reviewId": "bad", "reviewKind": "CONTEXT"},
                       {"confirmationStatus": "PENDING", "reviewId": REVIEW, "reviewKind": "CONTEXT", "liveId": USER},
                       {"status": "NEEDS_REVIEW"}, None):
            await self.prepare(route="PII_INVESTIGATION", verdict="NEEDS_REVIEW")
            async def submit(**kwargs):
                return result
            self.backend.submit_analysis = submit
            state = await self.start()
            self.assertEqual(state["error"]["code"], "INVALID_BACKEND_RESULT")
            self.assertIsNone(await pending_review(self.graph, self.task.thread_id))
            self.assertEqual(self.backend.calls, [])

    async def test_pii_defer_then_violation_preserves_same_review_and_proposal(self):
        await self.prepare(route="PII_INVESTIGATION", verdict="NEEDS_REVIEW")
        await self.start()
        self.backend.decision = "DEFER"
        await self.resume()
        self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewId"], REVIEW)
        self.assertEqual([c[0] for c in self.backend.calls], ["submit", "decision"])
        self.backend.decision = "CONFIRM_VIOLATION"
        await self.resume(decision=USER)
        final = (await self.graph.aget_state(self.config)).values
        self.assertEqual(final["message_findings"][0].verdict, "NEEDS_REVIEW")
        self.assertEqual(final["context_result"]["decision"], "CONFIRM_VIOLATION")
        self.assertEqual(sum(c[0] == "submit" for c in self.backend.calls), 1)
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_flood_policy_pending_and_pass_skip_model(self):
        for route, expected in (("FLOOD_REVIEW", "NEEDS_REVIEW"), ("PASS", "SUCCEEDED")):
            await self.prepare(route=route)
            state = await self.start()
            self.assertEqual(state["task_outcome"], expected)
            self.assertEqual(self.model.inputs, [])
            self.assertEqual(self.backend.calls, [])

    async def test_rule_confirmed_still_reaches_kick_without_model(self):
        await self.prepare(route="RULE_CONFIRMED", confirmationStatus="CONFIRMED",
                           kickReview={"required": True, "reviewId": KICK,
                                       "basisCaseIds": [REVIEW, DECISION, FIXTURE["task"]["caseId"]]})
        await self.start()
        self.assertEqual(self.model.inputs, [])
        self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewKind"], "KICK")

    async def test_incomplete_backend_contract_does_not_invent_live_or_snapshot(self):
        await self.prepare()
        self.business.fixture["case"].pop("liveId")
        state = await self.start()
        self.assertEqual(state["error"]["code"], "BACKEND_CONTRACT_INCOMPLETE")
        self.assertEqual(self.model.inputs, [])
        self.assertEqual(self.backend.calls, [])

    async def test_budget_exhaustion_creates_honest_manual_review_without_extra_model_calls(self):
        await self.prepare()
        result = await self.graph.ainvoke({"task": self.task, "harness_budget": {"modelCalls": 8}}, self.config)
        self.assertIsNone(result["task_outcome"])
        self.assertEqual(result["harness_budget"]["modelCalls"], 8)
        self.assertEqual([c[0] for c in self.backend.calls], ["create"])
        self.assertEqual((await pending_review(self.graph,self.task.thread_id))["reviewId"],REVIEW)
        self.assertEqual(result["message_findings"][0].verdict,"NEEDS_REVIEW")
        self.assertEqual(result["message_findings"][0].policy_ids,[])
        self.assertEqual(result["investigation_error"]["code"],"EXECUTION_LIMIT")
        await self.resume()
        self.assertEqual((await self.graph.aget_state(self.config)).values["task_outcome"],"SUCCEEDED")

    async def test_unverified_model_evidence_is_discarded_and_sent_to_human(self):
        await self.prepare(verdict="VIOLATION")
        forged = {**finding(self.state, "VIOLATION"), "evidenceMessageIds": [str(uuid4())]}
        self.model.calls = [search(), finish([forged])]
        result = await self.start()
        self.assertIsNone(result["task_outcome"])
        self.assertEqual(result["investigation_error"]["code"], "INVALID_EVIDENCE")
        self.assertEqual(result["message_findings"][0].verdict, "NEEDS_REVIEW")
        self.assertEqual(result["message_findings"][0].policy_ids, [])
        self.assertEqual(result["message_findings"][0].categories, [])
        self.assertEqual([call[0] for call in self.backend.calls], ["create"])
        self.assertEqual((await pending_review(self.graph,self.task.thread_id))["reviewId"], REVIEW)

    async def test_unknown_apply_response_stops_without_kick(self):
        await self.prepare()
        await self.start()
        async def unknown(**kwargs):
            return {**self.backend.refs(), "observation": "UNKNOWN", "status": "RUNNING"}
        self.backend.apply_context = unknown
        await self.resume()
        state = (await self.graph.aget_state(self.config)).values
        self.assertEqual(state["task_outcome"], "UNKNOWN")
        self.assertNotIn("kick", [c[0] for c in self.backend.calls])

    async def test_unconfirmed_rule_case_never_requests_kick(self):
        await self.prepare(route="RULE_CONFIRMED")
        result = await self.start()
        self.assertEqual(result["error"]["code"], "UNCONFIRMED_RULE_CASE")
        self.assertIsNone(await pending_review(self.graph, self.task.thread_id))

    async def test_v1_confirmation_status_preserves_rule_gate(self):
        for status in ("PENDING", "CONFIRMED_NORMAL", "CANCELLED"):
            await self.prepare(route="RULE_CONFIRMED", confirmationStatus=status)
            state = await self.start()
            self.assertEqual(state["error"]["code"], "UNCONFIRMED_RULE_CASE")
            self.assertEqual(self.backend.calls, [])
        await self.prepare(route="RULE_CONFIRMED", confirmationStatus="CONFIRMED_VIOLATION",
                           kickReview={"required": True, "reviewId": KICK,
                                       "basisCaseIds": [REVIEW, DECISION, FIXTURE["task"]["caseId"]]})
        await self.start()
        self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewKind"], "KICK")
        self.assertEqual(self.model.inputs, [])

    async def test_human_card_masks_text_and_preserves_evidence_ids(self):
        await self.prepare()
        self.business.fixture["context"]["messages"][0]["text"] = "연락처 010-1234-5678"
        await self.start()
        pending = await pending_review(self.graph, self.task.thread_id)
        self.assertNotIn("010-1234-5678", json.dumps(pending))
        self.assertEqual(pending["targetMessages"][0]["messageId"], FIXTURE["case"]["targetMessageIds"][0])

    async def test_cross_live_kick_revalidation_never_executes(self):
        await self.prepare(route="RULE_CONFIRMED", confirmationStatus="CONFIRMED",
                           kickReview={"required": True, "reviewId": KICK,
                                       "basisCaseIds": [REVIEW, DECISION, FIXTURE["task"]["caseId"]]})
        async def wrong(**kwargs):
            return {**self.backend.refs(), "liveId": USER, "status": "APPROVED", "actionCommand": {}}
        self.backend.revalidate = wrong
        await self.start()
        await self.resume(kind="KICK", review=KICK)
        self.assertNotIn("kick", [c[0] for c in self.backend.calls])
        self.assertEqual((await self.graph.aget_state(self.config)).values["task_outcome"], "FAILED")

    async def test_approved_kick_is_separate_and_duplicate_resume_is_rejected(self):
        await self.prepare()
        self.backend.decision, self.backend.require_kick = "CONFIRM_VIOLATION", True
        async def approve(**kwargs):
            return {**self.backend.refs(), "reviewId": KICK, "decisionId": USER, "status": "APPROVED",
                    "actionCommand": {"actionId": DECISION, "planId": REVIEW, "type": "KICK",
                                      "caseId": self.state["case_id"], "targetId": USER,
                                      "decisionId": USER, "expectedEvidenceVersion": 1,
                                      "expectedTargetVersion": 1, "idempotencyKey": DECISION,
                                      "violationIds": [REVIEW, DECISION, self.state["case_id"]],
                                      "policyVersion": self.state["policy_version"]}}
        async def apply(command, **kwargs):
            self.backend.calls.append(("kick", command))
            return {"actionId": DECISION, "status": "APPLIED"}
        self.backend.revalidate, self.backend.kick = approve, apply
        await self.start()
        await self.resume()
        self.assertNotIn("kick", [c[0] for c in self.backend.calls])
        await self.resume(kind="KICK", review=KICK, decision=USER)
        self.assertEqual((await self.graph.aget_state(self.config)).values["kick_result"]["kicked"], True)
        with self.assertRaises(ResumeError):
            await self.resume(kind="KICK", review=KICK, decision=USER)
        self.assertEqual(sum(c[0] == "kick" for c in self.backend.calls), 1)

    async def test_history_query_has_no_unapproved_thirty_day_default(self):
        from app.investigation.model import ToolCall
        await self.prepare()
        seen = []
        original = self.business.call
        async def call(name, arguments):
            if name == "get_user_history":
                seen.append(arguments)
                return {"data": {"windowHours": None}, "traceId": arguments["traceId"]}
            return await original(name, arguments)
        self.business.call = call
        self.model.calls.insert(0, ToolCall("history", "get_user_history", {}))
        await self.start()
        self.assertEqual(len(seen), 1)
        self.assertNotIn("days", seen[0])


class EventContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_b1_wire_uses_service_token_and_drops_tool_arguments(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(202)
        async with httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(handler)) as client:
            events = BackendEvents(client, service_token="fixture-service-token")
            await events.publish(case_id=REVIEW, trace_id=DECISION, event="TOOL_END",
                payload={"tool": "search_policy", "callId": "call-1", "elapsedMs": 14,
                         "arguments": {"text": "must not be sent"}})
            self.assertNotIn("fixture-service-token", repr(events))
        wire = json.loads(requests[0].content)
        self.assertNotIn("arguments", wire["payload"])
        self.assertEqual(requests[0].headers["X-Trace-Id"], DECISION)
        self.assertEqual(requests[0].headers["X-Service-Token"], "fixture-service-token")
        self.assertNotIn("Authorization", requests[0].headers)
        self.assertEqual(requests[0].url.path, f"/internal/cases/{REVIEW}/events")

    async def test_delivery_loss_is_unknown_without_automatic_retry(self):
        attempts = []
        def handler(request):
            attempts.append(request)
            raise httpx.ReadTimeout("synthetic", request=request)
        async with httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(AgentToolError) as raised:
                await BackendEvents(client, service_token="test").publish(
                    case_id=REVIEW, trace_id=DECISION, event="TASK_FAILED",
                    payload={"stage": "investigate", "errorCode": "EXECUTION_LIMIT"})
        self.assertEqual(raised.exception.code, "EVENT_DELIVERY_UNKNOWN")
        self.assertEqual(len(attempts), 1)


@unittest.skipUnless(os.getenv("AGENT_TEST_CHECKPOINT_DB_URL"), "Postgres integration DB required")
class WorkflowRestartTests(unittest.IsolatedAsyncioTestCase):
    prepare = WorkflowTests.prepare
    start = WorkflowTests.start
    resume = WorkflowTests.resume

    async def test_pii_context_defer_survives_restarts_without_resubmission(self):
        import psycopg
        from psycopg import sql
        await self.prepare(route="PII_INVESTIGATION", verdict="NEEDS_REVIEW")
        url = os.environ["AGENT_TEST_CHECKPOINT_DB_URL"]
        schema = "agent_pii_" + uuid4().hex[:10]
        def graph(saver):
            return build_moderation_graph(model=self.model, business=self.business, policy=Policies(),
                                          backend=self.backend, checkpointer=saver)
        try:
            async with open_checkpointer(url, schema=schema) as saver:
                self.graph = graph(saver)
                await self.start()
                self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewId"], REVIEW)
            self.backend.decision = "DEFER"
            async with open_checkpointer(url, schema=schema) as saver:
                self.graph = graph(saver)
                await self.resume()
                self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewId"], REVIEW)
            self.backend.decision = "CONFIRM_NORMAL"
            async with open_checkpointer(url, schema=schema) as saver:
                self.graph = graph(saver)
                await self.resume(decision=USER)
                final = (await self.graph.aget_state(self.config)).values
                self.assertEqual(final["task_outcome"], "SUCCEEDED")
                self.assertEqual(final["handling_route"], "PII_INVESTIGATION")
            self.assertEqual([c[0] for c in self.backend.calls], ["submit", "decision", "decision", "apply"])
            self.assertEqual(len(self.model.inputs), 2)
        finally:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    async def test_integrated_context_then_kick_survives_two_restarts(self):
        import psycopg
        from psycopg import sql
        await self.prepare()
        self.backend.decision, self.backend.require_kick = "CONFIRM_VIOLATION", True
        url = os.environ["AGENT_TEST_CHECKPOINT_DB_URL"]
        schema = "agent_workflow_" + uuid4().hex[:10]
        def graph(saver):
            return build_moderation_graph(model=self.model, business=self.business, policy=Policies(),
                                          backend=self.backend, checkpointer=saver)
        try:
            async with open_checkpointer(url, schema=schema) as saver:
                self.graph = graph(saver)
                await self.start()
            async with open_checkpointer(url, schema=schema) as saver:
                self.graph = graph(saver)
                await self.resume()
                self.assertEqual((await pending_review(self.graph, self.task.thread_id))["reviewKind"], "KICK")
            async with open_checkpointer(url, schema=schema) as saver:
                self.graph = graph(saver)
                await self.resume(kind="KICK", review=KICK, decision=USER)
                final = (await self.graph.aget_state(self.config)).values
                self.assertEqual(final["kick_result"]["status"], "REJECTED")
                self.assertEqual(final["harness_budget"]["modelCalls"], 2)
            self.assertEqual(len(self.model.inputs), 2)
            self.assertEqual(sum(c[0] == "create" for c in self.backend.calls), 1)
        finally:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
