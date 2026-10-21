"""Durable inbox/auth/runner tests with controlled Backend; no real sanctions."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.main import create_app
from app.harness.checkpoint import open_checkpointer
from app.integration.workflow import build_moderation_graph
from app.runtime.runner import TaskRunner
from app.runtime.store import TaskConflict, TaskStore, operation_id
from app.schemas import TaskEnvelope
from runtime_support import AUTH, test_application, wait_for_task
from test_agent_read import FIXTURE
import test_workflow_integration as workflow
from test_workflow_integration import REVIEW, DECISION, KICK, USER


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = f"{self.directory.name}/inbox.db"
        self.store = TaskStore(self.path)
        self.task = TaskEnvelope.model_validate(FIXTURE["task"])

    def test_atomic_duplicate_delivery_across_connections_and_reopen(self):
        def accept(_):
            return TaskStore(self.path).accept(self.task)[1]
        with ThreadPoolExecutor(max_workers=8) as pool:
            created = list(pool.map(accept, range(20)))
        self.assertEqual(sum(created), 1)
        self.assertEqual(TaskStore(self.path).get(self.task.task_id)["status"], "QUEUED")
        self.assertIsNotNone(self.store.claim())
        self.assertIsNone(TaskStore(self.path).claim())
        # Process crash leaves RUNNING blocked; no blind business replay on reopen.
        self.assertEqual(TaskStore(self.path).get(self.task.task_id)["status"], "RUNNING")

    def test_changed_payload_and_thread_reuse_rejected(self):
        self.store.accept(self.task)
        for change in ({"snapshot_id": uuid4()}, {"thread_id": "other"},
                       {"task_id": uuid4()}, {"case_id": uuid4()}):
            with self.assertRaises(TaskConflict):
                self.store.accept(self.task.model_copy(update=change))
        _, created = self.store.accept(self.task.model_copy(update={"event_id": uuid4(), "trace_id": uuid4()}))
        self.assertFalse(created)

    def test_queued_job_survives_reopen_and_has_one_claim(self):
        self.store.accept(self.task)
        reopened = TaskStore(self.path)
        self.assertEqual(reopened.claim()["operation_id"], operation_id(self.task))
        self.assertIsNone(self.store.claim())

    def test_resume_bound_to_original_snapshot_case_and_task(self):
        self.store.accept(self.task)
        self.store.finish(operation_id(self.task), "WAITING_REVIEW")
        resume = self.task.model_copy(update={"kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
                                             "review_id": UUID(REVIEW), "decision_id": UUID(DECISION)})
        for change in ({"snapshot_id": uuid4()}, {"case_id": uuid4()}, {"task_id": uuid4()},
                       {"expected_evidence_version": 2}, {"thread_id": "wrong"}):
            with self.assertRaises(TaskConflict):
                self.store.accept(resume.model_copy(update=change))
        self.store.accept(resume)
        with self.assertRaises(TaskConflict):
            self.store.accept(resume.model_copy(update={"decision_id": uuid4()}))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = f"{self.directory.name}/tasks.db"
        await workflow.WorkflowTests.prepare(self)
        self.store = TaskStore(self.path)
        self.runner = TaskRunner(self.store, self.graph)

    def decision(self, *, kind="CONTEXT", review=REVIEW, decision=DECISION):
        return self.task.model_copy(update={"kind": "RESUME_REVIEW", "review_kind": kind,
            "review_id": UUID(review), "decision_id": UUID(decision), "event_id": uuid4()})

    async def test_context_then_separate_kick_rejection_no_reinvestigation(self):
        self.store.accept(self.task)
        await self.runner.run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual([c[0] for c in self.backend.calls], ["create"])
        self.backend.decision = "CONFIRM_VIOLATION"
        self.backend.require_kick = True
        self.store.accept(self.decision())
        # Recreate inbox/runner while the checkpoint retains pending CONTEXT.
        runner = TaskRunner(TaskStore(self.path), self.graph)
        await runner.run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")
        self.store.accept(self.decision(kind="KICK", review=KICK, decision=USER))
        await runner.run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")
        self.assertNotIn("kick", [c[0] for c in self.backend.calls])
        self.assertFalse(await runner.run_one())
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_defer_and_bad_review_keep_waiting_without_apply(self):
        self.store.accept(self.task)
        await self.runner.run_one()
        self.store.accept(self.decision(kind="KICK"))
        await self.runner.run_one()
        self.assertEqual(self.store.get(self.task.task_id)["code"], "REVIEW_KIND_MISMATCH")
        self.backend.decision = "DEFER"
        self.store.accept(self.decision(decision=USER))
        await self.runner.run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual([c[0] for c in self.backend.calls], ["create", "decision"])

    async def test_transport_failure_is_unknown_and_never_replayed(self):
        self.store.accept(self.task)
        with patch.object(self.graph, "ainvoke", side_effect=RuntimeError("secret chat content")):
            await self.runner.run_one()
        record = self.store.get(self.task.task_id)
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertNotIn("secret", json.dumps(record))
        self.assertFalse(self.store.accept(self.task)[1])
        self.assertFalse(await self.runner.run_one())

    async def test_existing_checkpoint_cannot_be_overwritten(self):
        await self.graph.ainvoke({"task": self.task}, self.config)
        self.store.accept(self.task)
        await self.runner.run_one()
        self.assertEqual(self.store.get(self.task.task_id)["code"], "THREAD_CHECKPOINT_EXISTS")
        self.assertEqual(len(self.backend.calls), 1)

    async def test_http_auth_accept_poll_duplicate_and_store_failure(self):
        application, store = test_application(self.path, self.graph)
        body = self.task.model_dump(mode="json", by_alias=True)
        url = f"/agent/tasks/{self.task.task_id}/run"
        with TestClient(application) as client:
            self.assertTrue(client.get("/health").json()["readyForTasks"])
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-Service-Token": "test-inbound-token"}):
                self.assertEqual(client.post(url, json=body, headers=headers).status_code, 401)
                self.assertEqual(client.get(url.removesuffix("/run"), headers=headers).status_code, 401)
            self.assertIsNone(store.get(self.task.task_id))
            self.assertEqual(client.post(url, json=body, headers=AUTH).status_code, 202)
            self.assertEqual(wait_for_task(client, self.task.task_id)["status"], "WAITING_REVIEW")
            duplicate = client.post(url, json={**body, "eventId": str(uuid4())}, headers=AUTH)
            self.assertEqual(duplicate.status_code, 200)
            self.assertTrue(duplicate.json()["duplicate"])
            changed = {**body, "snapshotId": str(uuid4())}
            self.assertEqual(client.post(url, json=changed, headers=AUTH).status_code, 409)
            with patch.object(store, "accept", side_effect=__import__('sqlite3').OperationalError("secret")):
                response = client.post(url, json=body, headers=AUTH)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("secret", response.text)
        self.assertEqual([c[0] for c in self.backend.calls], ["create"])

    async def test_worker_cancellation_keeps_unknown_without_retry(self):
        self.store.accept(self.task)
        entered = asyncio.Event()
        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(self.graph, "ainvoke", side_effect=wait):
            worker = asyncio.create_task(self.runner.run_one())
            await entered.wait()
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        self.assertEqual(self.store.get(self.task.task_id)["status"], "UNKNOWN")
        self.assertFalse(await self.runner.run_one())

    async def test_empty_token_never_starts_worker_or_executes_saved_job(self):
        self.store.accept(self.task)
        @asynccontextmanager
        async def factory():
            yield self.runner
        with TestClient(create_app(runtime_factory=factory, service_token="")) as client:
            self.assertFalse(client.get("/health").json()["readyForTasks"])
            self.assertEqual(client.get(f"/agent/tasks/{self.task.task_id}", headers=AUTH).status_code, 503)
            self.assertIsNone(self.runner.worker)
        self.assertEqual(self.store.get(self.task.task_id)["status"], "QUEUED")
        self.assertEqual(self.backend.calls, [])

    async def test_not_ready_without_gateway_and_fail_closed_without_token(self):
        @asynccontextmanager
        async def missing():
            yield None
        with TestClient(create_app(runtime_factory=missing, service_token="test-inbound-token")) as client:
            self.assertFalse(client.get("/health").json()["readyForTasks"])
            response = client.post(f"/agent/tasks/{self.task.task_id}/run", headers=AUTH,
                                  json=self.task.model_dump(mode="json", by_alias=True))
            self.assertEqual(response.status_code, 503)

        @asynccontextmanager
        async def configured():
            yield self.runner
        with TestClient(create_app(runtime_factory=configured, service_token="")) as client:
            self.assertFalse(client.get("/health").json()["readyForTasks"])
            self.assertIsNone(self.runner.worker)
            self.assertEqual(client.get(f"/agent/tasks/{self.task.task_id}", headers=AUTH).status_code, 503)

    async def test_stopped_worker_keeps_saved_status_readable_but_rejects_work(self):
        self.store.accept(self.task)
        await self.runner.run_one()
        @asynccontextmanager
        async def configured():
            yield self.runner
        with TestClient(create_app(runtime_factory=configured, service_token="test-inbound-token")) as client:
            self.runner.failure = True
            self.assertFalse(client.get("/health").json()["readyForTasks"])
            response = client.get(f"/agent/tasks/{self.task.task_id}", headers=AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "WAITING_REVIEW")
            response = client.post(f"/agent/tasks/{self.task.task_id}/run", headers=AUTH,
                                  json=self.task.model_dump(mode="json", by_alias=True))
            self.assertEqual(response.status_code, 503)


@unittest.skipUnless(os.getenv("AGENT_TEST_CHECKPOINT_DB_URL"), "Postgres checkpoint DB required")
class PostgresRunnerRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_job_and_checkpoint_survive_connections_closed_then_resume(self):
        import psycopg
        from psycopg import sql
        from test_investigation import Policies
        await workflow.WorkflowTests.prepare(self)
        db_url = os.environ["AGENT_TEST_CHECKPOINT_DB_URL"]
        schema = f"agent_test_{uuid4().hex[:8]}"
        def build(saver):
            return build_moderation_graph(model=self.model, business=self.business,
                policy=Policies(), backend=self.backend, checkpointer=saver)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = f"{directory}/tasks.db"
                async with open_checkpointer(db_url, schema=schema) as saver:
                    store = TaskStore(path)
                    store.accept(self.task)
                    await TaskRunner(store, build(saver)).run_one()
                    self.assertEqual(store.get(self.task.task_id)["status"], "WAITING_REVIEW")
                # All checkpoint connections closed. New saver/Graph/inbox/runner.
                async with open_checkpointer(db_url, schema=schema) as saver:
                    store = TaskStore(path)
                    task = self.task.model_copy(update={"kind": "RESUME_REVIEW",
                        "review_kind": "CONTEXT", "review_id": UUID(REVIEW),
                        "decision_id": UUID(DECISION)})
                    store.accept(task)
                    await TaskRunner(store, build(saver)).run_one()
                    self.assertEqual(store.get(task.task_id)["status"], "SUCCEEDED")
                    self.assertEqual([c[0] for c in self.backend.calls], ["create", "decision", "apply"])
        finally:
            with psycopg.connect(db_url, autocommit=True) as db:
                db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
