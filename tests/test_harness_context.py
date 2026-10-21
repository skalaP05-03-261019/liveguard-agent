"""#24 제출 증거: 문맥 의심 경로는 AI가 정상 제안이어도 운영자 결정 전까지 멈춘다.
정상 확정 / 위반 확정 / 보류 후 재결정 / 중복·종류 불일치 거부 / 재시작 후 대기 복원."""

import os
import tempfile
import unittest
import uuid
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import sql

from app.harness.checkpoint import open_checkpointer
from app.harness.context_review import (
    CONTEXT_DECISION_REQUIRED, CONTEXT_DECISIONS, build_context_review_graph,
)
from app.harness.resume import (
    ResumeError, pending_context_review, pending_kick_approval, resume_context_review,
    resume_kick_review, thread_config,
)
from app.main import app as agent_app
from runtime_support import AUTH, test_application, wait_for_task

CASE_ID = "00000000-0000-4000-8000-0000000000c1"
TASK_ID = "00000000-0000-4000-8000-0000000000a1"
TRACE_ID = "00000000-0000-4000-8000-0000000000e1"
REVIEW_ID = "00000000-0000-4000-8000-0000000000b2"
NORMAL = uuid.UUID("00000000-0000-4000-8000-0000000000f1")
VIOLATION = uuid.UUID("00000000-0000-4000-8000-0000000000f2")
DEFER = uuid.UUID("00000000-0000-4000-8000-0000000000f3")
UNSAVED = uuid.UUID("00000000-0000-4000-8000-0000000000f4")


class FakeContextGateway:
    def __init__(self, review_id: str = REVIEW_ID) -> None:
        self.review_id = review_id
        self.decisions = {
            str(NORMAL): {"status": "DECIDED", "decision": "CONFIRM_NORMAL"},
            str(VIOLATION): {"status": "DECIDED", "decision": "CONFIRM_VIOLATION",
                             "categories": ["HARASSMENT"]},
            str(DEFER): {"status": "DECIDED", "decision": "DEFER"},
        }
        self.create_calls: list[dict[str, Any]] = []
        self.decision_calls: list[str] = []

    async def create_review(self, *, card, trace_id):
        self.create_calls.append(card)
        return {"reviewId": self.review_id}

    async def get_decision(self, *, review_id, decision_id, trace_id):
        self.decision_calls.append(decision_id)
        return self.decisions.get(decision_id, {"status": "INVALID", "reason": "NOT_FOUND"})


def context_state(*, route: str = "CONTEXT_REVIEW") -> dict[str, Any]:
    return {
        "case_id": CASE_ID,
        "task_id": TASK_ID,
        "trace_id": TRACE_ID,
        "snapshot_id": "00000000-0000-4000-8000-000000000051",
        "evidence_version": 1,
        "policy_version": "3.1",
        "handling_route": route,
        "target_messages": [{"messageId": "m-3", "authorAlias": "u***", "text": "너 진짜 대단하다 ㅋㅋ"}],
        "context_messages": [{"messageId": "m-2", "authorAlias": "v***", "text": "방금 그거 봤어?"}],
        # AI 제안은 정상이다. 그래도 문맥 경로에서는 공개 명령이 아니다(CONTRACTS §5).
        "message_findings": [
            {"messageId": "m-3", "verdict": "NO_VIOLATION", "categories": [], "policyIds": ["CP-3.2"],
             "reason": "문맥상 칭찬", "evidenceMessageIds": [], "missingFacts": []},
        ],
    }


class ContextReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.gateway = FakeContextGateway()
        self.graph = build_context_review_graph(self.gateway, checkpointer=InMemorySaver())
        self.thread = f"case:{CASE_ID}:analysis:{uuid.uuid4().hex[:6]}"

    async def start(self, state: dict[str, Any]) -> dict[str, Any]:
        return await self.graph.ainvoke({**state, "thread_id": self.thread}, thread_config(self.thread))

    async def resume(self, decision_id: uuid.UUID, review_id: str = REVIEW_ID) -> dict[str, Any]:
        return await resume_context_review(
            self.graph, thread_id=self.thread, review_id=uuid.UUID(review_id),
            decision_id=decision_id, case_id=uuid.UUID(CASE_ID),
        )

    async def values(self) -> dict[str, Any]:
        return (await self.graph.aget_state(thread_config(self.thread))).values

    async def test_non_context_route_passes_without_review(self) -> None:
        result = await self.start(context_state(route="RULE_CONFIRMED"))
        self.assertNotIn("__interrupt__", result)
        self.assertIsNone(await pending_context_review(self.graph, self.thread))
        self.assertEqual((self.gateway.create_calls, self.gateway.decision_calls), ([], []))

    async def test_normal_ai_suggestion_still_waits_for_operator(self) -> None:
        result = await self.start(context_state())
        self.assertIn("__interrupt__", result)
        card = await pending_context_review(self.graph, self.thread)
        self.assertEqual(card["type"], CONTEXT_DECISION_REQUIRED)
        self.assertEqual((card["reviewKind"], card["reviewId"]), ("CONTEXT", REVIEW_ID))
        self.assertEqual(card["findings"][0]["verdict"], "NO_VIOLATION")
        self.assertEqual(card["allowedDecisions"], list(CONTEXT_DECISIONS))
        self.assertEqual((card["policyVersion"], card["evidenceVersion"]), ("3.1", 1))
        self.assertEqual(len(self.gateway.create_calls), 1)
        self.assertNotIn("context_result", await self.values())

    async def test_confirm_normal_keeps_model_findings(self) -> None:
        await self.start(context_state())
        result = await self.resume(NORMAL)
        self.assertEqual(result, {"status": "DECIDED", "decision": "CONFIRM_NORMAL",
                                  "reviewId": REVIEW_ID, "decisionId": str(NORMAL)})
        values = await self.values()
        self.assertEqual((values["context_review_id"], values["context_decision_id"]),
                         (REVIEW_ID, str(NORMAL)))
        # 사람 결정으로 AI 제안을 덮어쓰지 않는다.
        self.assertEqual(values["message_findings"][0]["verdict"], "NO_VIOLATION")

    async def test_confirm_violation_carries_stored_categories(self) -> None:
        await self.start(context_state())
        result = await self.resume(VIOLATION)
        self.assertEqual((result["decision"], result["categories"]), ("CONFIRM_VIOLATION", ["HARASSMENT"]))

    async def test_defer_waits_again_then_final_decision(self) -> None:
        await self.start(context_state())
        deferred = await self.resume(DEFER)
        self.assertEqual((deferred["status"], deferred["decision"]), ("DEFERRED", "DEFER"))
        card = await pending_context_review(self.graph, self.thread)
        self.assertEqual(card["reviewId"], REVIEW_ID)  # 같은 심사로 다시 대기
        final = await self.resume(NORMAL)
        self.assertEqual(final["decision"], "CONFIRM_NORMAL")
        self.assertEqual(len(self.gateway.create_calls), 1)  # 검토 요청은 다시 만들지 않는다
        self.assertEqual(self.gateway.decision_calls, [str(DEFER), str(NORMAL)])

    async def test_duplicate_decision_is_not_applied_twice(self) -> None:
        await self.start(context_state())
        await self.resume(NORMAL)
        with self.assertRaises(ResumeError) as raised:
            await self.resume(NORMAL)
        self.assertEqual((raised.exception.code, raised.exception.status_code),
                         ("NO_PENDING_APPROVAL", 409))
        self.assertEqual(self.gateway.decision_calls, [str(NORMAL)])

    async def test_unsaved_decision_is_not_trusted(self) -> None:
        await self.start(context_state())
        result = await self.resume(UNSAVED)
        self.assertEqual(result["status"], "DECISION_NOT_VERIFIED")
        self.assertNotIn("decision", result)

    async def test_kick_decision_cannot_resume_context_review(self) -> None:
        await self.start(context_state())
        self.assertIsNone(await pending_kick_approval(self.graph, self.thread))
        with self.assertRaises(ResumeError) as wrong_kind:
            await resume_kick_review(self.graph, thread_id=self.thread, review_id=uuid.UUID(REVIEW_ID),
                                     decision_id=NORMAL, case_id=uuid.UUID(CASE_ID))
        self.assertEqual(wrong_kind.exception.code, "REVIEW_KIND_MISMATCH")
        with self.assertRaises(ResumeError) as wrong_review:
            await self.resume(NORMAL, review_id=str(uuid.uuid4()))
        self.assertEqual(wrong_review.exception.code, "REVIEW_MISMATCH")
        self.assertEqual(self.gateway.decision_calls, [])

    async def test_invalid_review_id_from_backend_does_not_pause(self) -> None:
        self.gateway.review_id = "not-a-uuid"
        result = await self.start(context_state())
        self.assertNotIn("__interrupt__", result)
        self.assertEqual(result["context_result"]["status"], "INVALID_REVIEW_REQUEST")


class ContextResumeEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        agent_app.state.agent_graph = None

    async def test_resume_endpoint_returns_context_result(self) -> None:
        graph = build_context_review_graph(FakeContextGateway(), checkpointer=InMemorySaver())
        thread = f"case:{CASE_ID}:analysis:http-context"
        await graph.ainvoke({**context_state(), "thread_id": thread}, thread_config(thread))
        agent_app.state.agent_graph = graph
        body = {
            "eventId": str(uuid.uuid4()), "taskId": TASK_ID, "caseId": CASE_ID,
            "kind": "RESUME_REVIEW", "reviewKind": "CONTEXT", "reviewId": REVIEW_ID,
            "threadId": thread, "snapshotId": str(uuid.uuid4()), "expectedEvidenceVersion": 1,
            "decisionId": str(NORMAL), "traceId": TRACE_ID,
        }
        with tempfile.TemporaryDirectory() as directory:
            application, _ = test_application(f"{directory}/tasks.db", graph, body)
            with TestClient(application) as client:
                first = client.post(f"/agent/tasks/{TASK_ID}/resume", json=body, headers=AUTH)
                wait_for_task(client, TASK_ID)
                second = client.post(f"/agent/tasks/{TASK_ID}/resume", json=body, headers=AUTH)
        self.assertEqual(first.status_code, 202)
        values = (await graph.aget_state(thread_config(thread))).values
        self.assertEqual(values["context_result"]["decision"], "CONFIRM_NORMAL")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])



@unittest.skipUnless(
    os.getenv("AGENT_TEST_CHECKPOINT_DB_URL"),
    "AGENT_TEST_CHECKPOINT_DB_URL이 없으면 Postgres 재시작 복원 테스트를 건너뛴다",
)
class ContextPostgresRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.db_url = os.environ["AGENT_TEST_CHECKPOINT_DB_URL"]
        self.schema = f"agent_test_{uuid.uuid4().hex[:8]}"
        self.thread = f"case:{CASE_ID}:analysis:context-restart"

    async def asyncTearDown(self) -> None:
        with psycopg.connect(self.db_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(self.schema)))

    async def test_waiting_context_review_survives_restart(self) -> None:
        async with open_checkpointer(self.db_url, schema=self.schema) as saver:
            graph = build_context_review_graph(FakeContextGateway(), checkpointer=saver)
            result = await graph.ainvoke({**context_state(), "thread_id": self.thread},
                                         thread_config(self.thread))
            self.assertIn("__interrupt__", result)

        gateway = FakeContextGateway()  # 재시작 후 새 프로세스
        async with open_checkpointer(self.db_url, schema=self.schema) as saver:
            graph = build_context_review_graph(gateway, checkpointer=saver)
            card = await pending_context_review(graph, self.thread)
            self.assertEqual(card["reviewId"], REVIEW_ID)
            deferred = await resume_context_review(graph, thread_id=self.thread, review_id=uuid.UUID(REVIEW_ID),
                                                   decision_id=DEFER, case_id=uuid.UUID(CASE_ID))
        self.assertEqual(deferred["status"], "DEFERRED")

        async with open_checkpointer(self.db_url, schema=self.schema) as saver:
            graph = build_context_review_graph(gateway, checkpointer=saver)
            final = await resume_context_review(graph, thread_id=self.thread, review_id=uuid.UUID(REVIEW_ID),
                                                decision_id=VIOLATION, case_id=uuid.UUID(CASE_ID))
        self.assertEqual(final["decision"], "CONFIRM_VIOLATION")
        self.assertEqual(gateway.create_calls, [])  # 재시작 후 검토 요청을 다시 만들지 않는다


if __name__ == "__main__":
    unittest.main()
