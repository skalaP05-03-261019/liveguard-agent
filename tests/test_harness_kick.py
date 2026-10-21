"""#5 제출 증거 (PROJECT AC-12~18):
2건 미요청 / 3건 중단·재시작 후 대기 / 승인→강퇴·거절→미강퇴 / 중복 미가산·중복 승인 1회."""

import os
import tempfile
import unittest
import uuid
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import sql

from app.graph import build_graph
from app.harness.checkpoint import allowed_state_types, open_checkpointer
from app.harness.kick_review import KICK_APPROVAL_REQUIRED, build_kick_review_graph
from app.harness.resume import ResumeError, pending_kick_approval, resume_kick_review, thread_config
from app.main import app as agent_app
from runtime_support import AUTH, test_application, wait_for_task

CASE_ID = "00000000-0000-4000-8000-0000000000c1"
TASK_ID = "00000000-0000-4000-8000-0000000000a1"
TRACE_ID = "00000000-0000-4000-8000-0000000000e1"
REVIEW_ID = "00000000-0000-4000-8000-0000000000b1"
APPROVE = uuid.UUID("00000000-0000-4000-8000-0000000000d1")
REJECT = uuid.UUID("00000000-0000-4000-8000-0000000000d2")
STALE = uuid.UUID("00000000-0000-4000-8000-0000000000d3")
C1, C2, C3 = (f"00000000-0000-4000-8000-00000000010{i}" for i in (1, 2, 3))


class FakeKickGateway:
    def __init__(self, kick_status: str = "APPLIED") -> None:
        self.statuses = {str(APPROVE): "APPROVED", str(REJECT): "REJECTED", str(STALE): "INVALID"}
        self.kick_status = kick_status
        self.revalidate_calls: list[dict[str, Any]] = []
        self.kick_calls: list[dict[str, Any]] = []

    async def revalidate(self, *, review_id, decision_id, trace_id):
        self.revalidate_calls.append({"reviewId": review_id, "decisionId": decision_id})
        status = self.statuses.get(decision_id, "INVALID")
        if status != "APPROVED":
            return {"status": status}
        return {"status": "APPROVED",
                "actionCommand": {"actionId": "act-1", "type": "KICK", "decisionId": decision_id}}

    async def kick(self, action_command, *, trace_id):
        self.kick_calls.append(action_command)
        return {"actionId": action_command["actionId"], "status": self.kick_status}


def case_state(*, required: bool, basis: list[str] | None = None,
               review_id: str = REVIEW_ID) -> dict[str, Any]:
    return {
        "case_id": CASE_ID,
        "task_id": TASK_ID,
        "thread_id": f"case:{CASE_ID}:analysis:1",
        "trace_id": TRACE_ID,
        "policy_version": "3.0",
        "target_messages": [{"messageId": "m-3", "authorAlias": "u***", "text": "[masked]"}],
        "context_messages": [{"messageId": "m-2", "authorAlias": "v***", "text": "안녕"}],
        "message_findings": [
            {"messageId": "m-3", "verdict": "VIOLATION", "categories": ["PROFANITY"],
             "policyIds": ["CP-2.1"], "reason": "사전 등록 욕설", "evidenceMessageIds": ["m-3"]},
        ],
        "kick_review": {"required": required, "reviewId": review_id, "basisCaseIds": basis or []},
    }


class KickReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.gateway = FakeKickGateway()
        self.graph = build_kick_review_graph(self.gateway, checkpointer=InMemorySaver())
        self.new_thread()

    def new_thread(self) -> None:
        self.thread = f"case:{CASE_ID}:analysis:{uuid.uuid4().hex[:6]}"
        self.config = thread_config(self.thread)

    async def start(self, state: dict[str, Any]) -> dict[str, Any]:
        return await self.graph.ainvoke({**state, "thread_id": self.thread}, self.config)

    async def resume(self, decision_id: uuid.UUID, review_id: str = REVIEW_ID) -> dict[str, Any]:
        return await resume_kick_review(
            self.graph, thread_id=self.thread, review_id=uuid.UUID(review_id),
            decision_id=decision_id, case_id=uuid.UUID(CASE_ID),
        )

    async def test_ac12_two_cases_do_not_request_approval(self) -> None:
        # Backend가 통합 2건이라 심사 불필요(required=false)로 보낸 경우
        result = await self.start(case_state(required=False, basis=[C1, C2]))
        self.assertNotIn("__interrupt__", result)
        self.assertIsNone(await pending_kick_approval(self.graph, self.thread))
        self.assertEqual((self.gateway.revalidate_calls, self.gateway.kick_calls), ([], []))

    async def test_ac13_three_cases_pause_with_operator_card(self) -> None:
        result = await self.start(case_state(required=True, basis=[C1, C2, C3]))
        self.assertIn("__interrupt__", result)
        payload = await pending_kick_approval(self.graph, self.thread)
        self.assertEqual(payload["type"], KICK_APPROVAL_REQUIRED)
        self.assertEqual((payload["reviewKind"], payload["reviewId"]), ("KICK", REVIEW_ID))
        self.assertEqual(payload["basisCaseIds"], [C1, C2, C3])
        self.assertEqual(payload["findings"][0]["policyIds"], ["CP-2.1"])
        self.assertEqual(payload["targetMessages"][0]["messageId"], "m-3")
        self.assertNotIn("durationSeconds", str(payload))
        self.assertEqual(self.gateway.kick_calls, [])

    async def test_kick_card_retains_snapshot_and_typed_finding_evidence(self) -> None:
        from app.schemas import MessageFinding
        state = case_state(required=True, basis=[C1, C2, C3])
        state.update(snapshot_id=CASE_ID, evidence_version=4)
        state["message_findings"] = [MessageFinding(
            message_id=uuid.UUID(CASE_ID), verdict="VIOLATION", categories=["HARASSMENT"],
            policy_ids=["CP-3.2"], evidence_message_ids=[uuid.UUID(CASE_ID)], reason="반복적인 표적 괴롭힘")]
        await self.start(state)
        card = await pending_kick_approval(self.graph, self.thread)
        self.assertEqual((card["snapshotId"], card["evidenceVersion"]), (CASE_ID, 4))
        self.assertEqual(card["findings"][0]["evidenceMessageIds"], [CASE_ID])
        self.assertEqual(card["findings"][0]["missingFacts"], [])
        self.assertEqual(card["basisCaseIds"], [C1, C2, C3])
        self.assertEqual(self.gateway.kick_calls, [])

    async def test_ac14_approval_kicks_after_revalidation(self) -> None:
        await self.start(case_state(required=True, basis=[C1, C2, C3]))
        kick_result = await self.resume(APPROVE)
        self.assertEqual((kick_result["status"], kick_result["kicked"]), ("APPLIED", True))
        self.assertEqual(self.gateway.revalidate_calls,
                         [{"reviewId": REVIEW_ID, "decisionId": str(APPROVE)}])
        self.assertEqual(len(self.gateway.kick_calls), 1)

    async def test_ac15_rejection_does_not_kick(self) -> None:
        await self.start(case_state(required=True, basis=[C1, C2, C3]))
        kick_result = await self.resume(REJECT)
        self.assertEqual((kick_result["status"], kick_result["kicked"]), ("REJECTED", False))
        self.assertEqual(kick_result["reviewId"], REVIEW_ID)
        self.assertEqual(self.gateway.kick_calls, [])

    async def test_ac18_duplicate_approval_does_not_kick_twice(self) -> None:
        await self.start(case_state(required=True, basis=[C1, C2, C3]))
        await self.resume(APPROVE)
        with self.assertRaises(ResumeError) as raised:
            await self.resume(APPROVE)
        self.assertEqual((raised.exception.code, raised.exception.status_code),
                         ("NO_PENDING_APPROVAL", 409))
        self.assertEqual(len(self.gateway.kick_calls), 1)

    async def test_ac17_duplicate_or_too_few_cases_are_not_counted(self) -> None:
        for basis in ([C1, C1, C2], [C1, C2], ["not-a-uuid", C1, C2]):
            with self.subTest(basis=basis):
                self.new_thread()
                result = await self.start(case_state(required=True, basis=basis))
                self.assertNotIn("__interrupt__", result)
                self.assertEqual(result["kick_result"]["status"], "INVALID_REVIEW_REQUEST")
        self.assertEqual(self.gateway.kick_calls, [])

    async def test_failed_revalidation_does_not_kick(self) -> None:
        # 취소·시간창 이탈·타 심사 소진 등으로 Backend 재검증 미통과
        await self.start(case_state(required=True, basis=[C1, C2, C3]))
        kick_result = await self.resume(STALE)
        self.assertEqual(kick_result["status"], "REVALIDATION_FAILED")
        self.assertEqual(self.gateway.kick_calls, [])

    async def test_unknown_kick_result_is_not_reported_as_failure(self) -> None:
        self.gateway.kick_status = "UNKNOWN"
        await self.start(case_state(required=True, basis=[C1, C2, C3]))
        kick_result = await self.resume(APPROVE)
        self.assertEqual(kick_result["status"], "UNKNOWN")
        self.assertIsNone(kick_result["kicked"])

    async def test_wrong_thread_case_or_review_is_rejected(self) -> None:
        with self.assertRaises(ResumeError) as missing:
            await self.resume(APPROVE)
        self.assertEqual(missing.exception.status_code, 404)
        await self.start(case_state(required=True, basis=[C1, C2, C3]))
        with self.assertRaises(ResumeError) as other_case:
            await resume_kick_review(self.graph, thread_id=self.thread, review_id=uuid.UUID(REVIEW_ID),
                                     decision_id=APPROVE, case_id=uuid.uuid4())
        self.assertEqual(other_case.exception.code, "CASE_MISMATCH")
        with self.assertRaises(ResumeError) as other_review:
            await self.resume(APPROVE, review_id=str(uuid.uuid4()))
        self.assertEqual(other_review.exception.code, "REVIEW_MISMATCH")
        self.assertEqual(self.gateway.kick_calls, [])


class GraphWiringTests(unittest.TestCase):
    def test_full_graph_runs_kick_review_after_verify_result(self) -> None:
        graph = build_graph(checkpointer=InMemorySaver(), kick_gateway=FakeKickGateway()).get_graph()
        edges = {(edge.source, edge.target) for edge in graph.edges}
        self.assertIn(("verify_result", "review_kick"), edges)
        self.assertIn(("review_kick", "execute_kick"), edges)
        self.assertIn(("execute_kick", "report_result"), edges)
        self.assertNotIn(("verify_result", "report_result"), edges)

    def test_checkpoint_allowlist_covers_state_contract_models(self) -> None:
        allowed = set(allowed_state_types())
        for name in ("TaskEnvelope", "MessageFinding", "JudgmentOutput", "Category", "Judgment"):
            self.assertIn(("app.schemas", name), allowed)


class ResumeEndpointTests(unittest.IsolatedAsyncioTestCase):
    def envelope(self, thread_id: str, *, review_kind: str | None = "KICK") -> dict[str, Any]:
        body = {
            "eventId": str(uuid.uuid4()), "taskId": TASK_ID, "caseId": CASE_ID,
            "kind": "RESUME_REVIEW", "threadId": thread_id, "snapshotId": str(uuid.uuid4()),
            "expectedEvidenceVersion": 1, "decisionId": str(APPROVE), "traceId": TRACE_ID,
            "reviewId": REVIEW_ID,
        }
        if review_kind:
            body["reviewKind"] = review_kind
        return body

    async def asyncTearDown(self) -> None:
        agent_app.state.agent_graph = None

    async def test_resume_endpoint_returns_kick_result_then_conflict(self) -> None:
        gateway = FakeKickGateway()
        graph = build_kick_review_graph(gateway, checkpointer=InMemorySaver())
        thread = f"case:{CASE_ID}:analysis:http"
        await graph.ainvoke({**case_state(required=True, basis=[C1, C2, C3]), "thread_id": thread},
                            thread_config(thread))
        agent_app.state.agent_graph = graph
        origin = self.envelope(thread)
        with tempfile.TemporaryDirectory() as directory:
            application, _ = test_application(f"{directory}/tasks.db", graph, origin)
            with TestClient(application) as client:
                no_kind = client.post(f"/agent/tasks/{TASK_ID}/resume", headers=AUTH,
                                      json={**origin, "reviewKind": None})
                context = client.post(f"/agent/tasks/{TASK_ID}/resume", headers=AUTH,
                                      json={**origin, "reviewKind": "CONTEXT", "decisionId": str(uuid.uuid4())})
                self.assertEqual(context.status_code, 202)
                self.assertEqual(wait_for_task(client, TASK_ID)["code"], "REVIEW_KIND_MISMATCH")
                valid = origin
                first = client.post(f"/agent/tasks/{TASK_ID}/resume", json=valid, headers=AUTH)
                wait_for_task(client, TASK_ID)
                second = client.post(f"/agent/tasks/{TASK_ID}/resume", json=valid, headers=AUTH)
        self.assertEqual(no_kind.status_code, 400)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(len(gateway.kick_calls), 1)

    async def test_resume_endpoint_requires_configured_service_auth(self) -> None:
        with TestClient(agent_app) as client:
            response = client.post(f"/agent/tasks/{TASK_ID}/resume", json=self.envelope("t"))
        self.assertEqual(response.status_code, 503)


@unittest.skipUnless(
    os.getenv("AGENT_TEST_CHECKPOINT_DB_URL"),
    "AGENT_TEST_CHECKPOINT_DB_URL이 없으면 Postgres 재시작 복원 테스트를 건너뛴다",
)
class PostgresRestartTests(unittest.IsolatedAsyncioTestCase):
    """프로세스 재시작을 checkpointer·Graph를 새로 만드는 것으로 재현한다 (AC-13, AC-18)."""

    async def asyncSetUp(self) -> None:
        self.db_url = os.environ["AGENT_TEST_CHECKPOINT_DB_URL"]
        self.schema = f"agent_test_{uuid.uuid4().hex[:8]}"
        self.thread = f"case:{CASE_ID}:analysis:restart"

    async def asyncTearDown(self) -> None:
        with psycopg.connect(self.db_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    async def run_until_interrupt(self) -> None:
        async with open_checkpointer(self.db_url, schema=self.schema) as saver:
            graph = build_kick_review_graph(FakeKickGateway(), checkpointer=saver)
            state = {**case_state(required=True, basis=[C1, C2, C3]), "thread_id": self.thread}
            result = await graph.ainvoke(state, thread_config(self.thread))
            self.assertIn("__interrupt__", result)

    async def resume_after_restart(self, gateway: FakeKickGateway, decision_id: uuid.UUID):
        async with open_checkpointer(self.db_url, schema=self.schema) as saver:
            graph = build_kick_review_graph(gateway, checkpointer=saver)
            return await resume_kick_review(graph, thread_id=self.thread, review_id=uuid.UUID(REVIEW_ID),
                                            decision_id=decision_id, case_id=uuid.UUID(CASE_ID))

    async def test_waiting_approval_survives_restart_then_approval_kicks_once(self) -> None:
        await self.run_until_interrupt()  # 첫 프로세스는 여기서 종료

        async with open_checkpointer(self.db_url, schema=self.schema) as saver:
            graph = build_kick_review_graph(FakeKickGateway(), checkpointer=saver)
            payload = await pending_kick_approval(graph, self.thread)
        self.assertEqual(payload["basisCaseIds"], [C1, C2, C3])

        gateway = FakeKickGateway()
        kick_result = await self.resume_after_restart(gateway, APPROVE)
        self.assertEqual(kick_result["status"], "APPLIED")
        with self.assertRaises(ResumeError):
            await self.resume_after_restart(gateway, APPROVE)  # 또 재시작 + 중복 승인
        self.assertEqual(len(gateway.kick_calls), 1)

    async def test_rejection_after_restart_does_not_kick(self) -> None:
        await self.run_until_interrupt()
        gateway = FakeKickGateway()
        kick_result = await self.resume_after_restart(gateway, REJECT)
        self.assertEqual(kick_result["status"], "REJECTED")
        self.assertEqual(gateway.kick_calls, [])


if __name__ == "__main__":
    unittest.main()
