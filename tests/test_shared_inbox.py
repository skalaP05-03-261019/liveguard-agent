"""Real PostgreSQL, independent API lifespans/connections; no model or sanctions."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime.inbox import PostgresInbox
from app.runtime.store import TaskConflict, operation_id
from app.schemas import TaskEnvelope
from test_agent_read import FIXTURE
from runtime_support import AUTH


def inbox_application(inbox):
    @asynccontextmanager
    async def factory():
        yield inbox

    @asynccontextmanager
    async def forbidden_runtime():
        raise AssertionError("Intake must not initialize a Graph runtime")
        yield

    return create_app(inbox_factory=factory, runtime_factory=forbidden_runtime,
                      service_token="test-inbound-token")


@unittest.skipUnless(os.getenv("AGENT_TEST_INBOX_DB_URL"), "Postgres Inbox DB required")
class SharedInboxTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ["AGENT_TEST_INBOX_DB_URL"]
        self.schema = f"inbox_test_{uuid4().hex}"
        self.first = PostgresInbox(self.url, schema=self.schema)
        self.second = PostgresInbox(self.url, schema=self.schema)
        self.addCleanup(self.drop_schema)
        # Both instances may start on a brand-new database at the same time.
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda inbox: inbox.setup(), (self.first, self.second)))
        self.task = TaskEnvelope.model_validate(FIXTURE["task"])
        self.body = self.task.model_dump(mode="json", by_alias=True)
        self.path = f"/agent/tasks/{self.task.task_id}"

    def drop_schema(self):
        with psycopg.connect(self.url, autocommit=True) as db:
            db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(self.schema)))

    def mark_waiting(self, task):
        # Execution status changes belong to the future shared worker, not intake.
        with self.first.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET status='WAITING_REVIEW' WHERE operation_id=%s")
                       .format(sql.Identifier(self.schema, "jobs")), (operation_id(task),))

    def test_concurrent_api_acceptance_duplicate_receipt_and_shared_status(self):
        barrier = threading.Barrier(2)
        with TestClient(inbox_application(self.first)) as a, \
                TestClient(inbox_application(self.second)) as b:
            self.assertTrue(a.get("/health").json()["readyForTasks"])
            self.assertTrue(b.get("/health").json()["readyForTasks"])
            self.assertEqual(a.post(self.path + "/run", json=self.body).status_code, 401)

            def post(client):
                barrier.wait(timeout=5)
                return client.post(self.path + "/run", json=self.body, headers=AUTH)

            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(post, (a, b)))
            self.assertEqual(sorted(r.status_code for r in replies), [200, 202])
            for response in replies:
                self.assertEqual(response.json()["duplicate"], response.status_code == 200)
                self.assertEqual(response.json()["operationId"], operation_id(self.task))
                self.assertEqual(response.json()["status"], "QUEUED")
            self.assertEqual(a.get(self.path, headers=AUTH).json(),
                             b.get(self.path, headers=AUTH).json())
            retry = b.post(self.path + "/run", json={
                **self.body, "eventId": str(uuid4()), "traceId": str(uuid4()),
            }, headers=AUTH)
            self.assertEqual(retry.status_code, 200)
            self.assertTrue(retry.json()["duplicate"])
        with self.first.connection() as db:
            count = db.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(
                sql.Identifier(self.schema, "jobs"))).fetchone()["n"]
        self.assertEqual(count, 1)

    def test_binding_conflicts_roll_back_without_extra_jobs(self):
        self.first.accept(self.task)
        for changes in (
            {"snapshot_id": uuid4()}, {"case_id": uuid4()},
            {"expected_evidence_version": 2}, {"thread_id": "wrong-thread"},
            {"task_id": uuid4()},
            {"thread_id": "different-thread", "case_id": uuid4()},
        ):
            with self.subTest(changes=changes), self.assertRaises(TaskConflict):
                self.second.accept(self.task.model_copy(update=changes))
        with TestClient(inbox_application(self.second)) as client:
            response = client.post(self.path + "/run", headers=AUTH,
                                   json={**self.body, "snapshotId": str(uuid4())})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["detail"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_resume_concurrency_binding_and_latest_operation(self):
        self.first.accept(self.task)
        resume = self.task.model_copy(update={
            "kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
            "review_id": uuid4(), "decision_id": uuid4(),
        })
        self.mark_waiting(self.task)
        for changes in (
            {"snapshot_id": uuid4()}, {"case_id": uuid4()}, {"task_id": uuid4()},
            {"expected_evidence_version": 2}, {"thread_id": "wrong-thread"},
        ):
            with self.subTest(changes=changes), self.assertRaises(TaskConflict):
                self.second.accept(resume.model_copy(update=changes))

        body = resume.model_dump(mode="json", by_alias=True)
        with TestClient(inbox_application(self.first)) as a, \
                TestClient(inbox_application(self.second)) as b:
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(
                    lambda client: client.post(self.path + "/resume", json=body, headers=AUTH),
                    (a, b)))
            self.assertEqual(sorted(r.status_code for r in replies), [200, 202])
            self.assertEqual(b.get(self.path, headers=AUTH).json()["operationId"],
                             operation_id(resume))
        _, created = self.second.accept(resume.model_copy(update={"decision_id": uuid4()}))
        self.assertTrue(created)
        with self.assertRaisesRegex(TaskConflict, "IDEMPOTENCY_CONFLICT"):
            self.second.accept(resume.model_copy(update={"review_id": uuid4()}))

    def test_distinct_resume_decisions_queue_in_thread_order(self):
        self.first.accept(self.task)
        self.mark_waiting(self.task)
        resumes = [self.task.model_copy(update={
            "kind": "RESUME_REVIEW", "review_kind": "KICK",
            "review_id": uuid4(), "decision_id": uuid4(),
        }) for _ in range(2)]
        barrier = threading.Barrier(2)

        def accept(pair):
            inbox, task = pair
            barrier.wait(timeout=5)
            try:
                return inbox.accept(task)[1]
            except TaskConflict as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, zip((self.first, self.second), resumes)))
        self.assertEqual(results, [True, True])
        first = self.first.claim("worker-a")
        self.assertIsNotNone(first)
        self.assertIsNone(self.second.claim("worker-b"))
        self.first.finish(first, "WAITING_REVIEW")
        second = self.second.claim("worker-b")
        self.assertIsNotNone(second)
        self.assertNotEqual(first["operation_id"], second["operation_id"])

    def test_early_resume_is_durable_but_not_claimable_ahead_of_run(self):
        self.first.accept(self.task)
        run = self.first.claim("worker-a")
        resume = self.task.model_copy(update={
            "kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
            "review_id": uuid4(), "decision_id": uuid4(),
        })
        receipt, created = self.second.accept(resume)
        self.assertTrue(created)
        self.assertEqual(receipt["status"], "QUEUED")
        self.assertIsNone(self.second.claim("worker-b"))
        self.first.finish(run, "WAITING_REVIEW")
        self.assertEqual(self.second.claim("worker-b")["operation_id"], operation_id(resume))

    def test_resume_intake_blocks_unknown_predecessors_but_keeps_duplicate_receipts(self):
        self.first.accept(self.task)
        run = self.first.claim("worker-a")
        resume = self.task.model_copy(update={
            "kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
            "review_id": uuid4(), "decision_id": uuid4(),
        })
        self.first.accept(resume)
        self.first.finish(run, "UNKNOWN")

        receipt, created = self.second.accept(resume)
        self.assertFalse(created)
        self.assertEqual(receipt["status"], "QUEUED")
        with self.assertRaisesRegex(TaskConflict, "THREAD_NOT_WAITING"):
            self.second.accept(resume.model_copy(update={"decision_id": uuid4()}))

    def test_failed_resume_allows_correction_only_while_approval_is_pending(self):
        self.first.accept(self.task)
        self.mark_waiting(self.task)
        failed = self.task.model_copy(update={
            "kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
            "review_id": uuid4(), "decision_id": uuid4(),
        })
        self.first.accept(failed)
        claimed = self.first.claim("worker-a")
        self.first.finish(claimed, "FAILED", "REVIEW_MISMATCH")
        correction = failed.model_copy(update={"decision_id": uuid4()})
        _, created = self.second.accept(correction)
        self.assertTrue(created)

    def test_failed_queued_resume_does_not_reopen_completed_thread(self):
        self.first.accept(self.task)
        self.mark_waiting(self.task)
        resume = self.task.model_copy(update={
            "kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
            "review_id": uuid4(), "decision_id": uuid4(),
        })
        self.first.accept(resume)
        queued_resume = resume.model_copy(update={"decision_id": uuid4()})
        self.first.accept(queued_resume)
        self.first.finish(self.first.claim("worker-a"), "SUCCEEDED")
        self.first.finish(self.first.claim("worker-b"), "FAILED", "NO_PENDING_APPROVAL")

        with self.assertRaisesRegex(TaskConflict, "THREAD_NOT_WAITING"):
            self.second.accept(resume.model_copy(update={"decision_id": uuid4()}))

    def test_unknown_and_backoff_heads_block_only_their_own_thread(self):
        for status in ("UNKNOWN", "BACKOFF"):
            with self.subTest(status=status):
                task = self.task.model_copy(update={
                    "task_id": uuid4(), "thread_id": "0-blocked-" + uuid4().hex})
                self.first.accept(task)
                run = self.first.claim("worker-a")
                self.first.accept(task.model_copy(update={
                    "kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
                    "review_id": uuid4(), "decision_id": uuid4(),
                }))
                if status == "UNKNOWN":
                    self.first.finish(run, "UNKNOWN")
                else:
                    self.first.retry_unstarted(run, backoff_seconds=300)
                other = self.task.model_copy(update={
                    "task_id": uuid4(), "thread_id": "z-ready-" + uuid4().hex})
                self.second.accept(other)
                claimed = self.second.claim("worker-b")
                self.assertEqual(claimed["thread_id"], other.thread_id)
                self.second.finish(claimed, "SUCCEEDED")
                self.assertIsNone(self.second.claim("worker-c"))

    def test_durable_receipt_survives_api_process_exit(self):
        code = """
import json, os, sys
from fastapi.testclient import TestClient
from app.runtime.inbox import PostgresInbox
from test_shared_inbox import inbox_application, AUTH
data = json.load(sys.stdin)
inbox = PostgresInbox(data["url"], schema=data["schema"])
with TestClient(inbox_application(inbox)) as client:
    response = client.post(data["path"] + "/run", json=data["body"], headers=AUTH)
    print(json.dumps({"status": response.status_code, "receipt": response.json()}), flush=True)
    os._exit(0)
"""
        result = subprocess.run([sys.executable, "-c", code], input=json.dumps({
            "url": self.url, "schema": self.schema, "path": self.path, "body": self.body,
        }), env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            text=True, capture_output=True, timeout=30, check=True)
        self.assertEqual(json.loads(result.stdout)["status"], 202)
        with TestClient(inbox_application(self.second)) as client:
            response = client.get(self.path, headers=AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "QUEUED")
            duplicate = client.post(self.path + "/run", json=self.body, headers=AUTH)
            self.assertEqual(duplicate.status_code, 200)
            self.assertTrue(duplicate.json()["duplicate"])

    def test_store_failure_is_explicit_and_never_a_receipt(self):
        with TestClient(inbox_application(self.first)) as client:
            with patch.object(self.first, "accept",
                              side_effect=psycopg.OperationalError("private connection detail")):
                response = client.post(self.path + "/run", json=self.body, headers=AUTH)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["detail"]["code"], "TASK_STORE_UNAVAILABLE")
                self.assertNotIn("private", response.text)
            self.assertIsNone(self.second.get(self.task.task_id))
            with patch.object(self.first, "get", side_effect=psycopg.OperationalError()):
                self.assertEqual(client.get(self.path, headers=AUTH).status_code, 503)
            with patch.object(self.first, "check", side_effect=psycopg.OperationalError()):
                self.assertFalse(client.get("/health").json()["readyForTasks"])

    def test_failed_transaction_exit_rolls_back_before_http_receipt(self):
        original = self.first.connection

        @contextmanager
        def failing_commit():
            with original() as db:
                yield db
                raise psycopg.OperationalError("commit not confirmed")

        with TestClient(inbox_application(self.first)) as client:
            with patch.object(self.first, "connection", failing_commit):
                response = client.post(self.path + "/run", json=self.body, headers=AUTH)
            self.assertEqual(response.status_code, 503)
            self.assertIsNone(self.second.get(self.task.task_id))
            self.assertEqual(client.post(self.path + "/run", json=self.body,
                                         headers=AUTH).status_code, 202)

    def test_default_application_uses_configured_shared_inbox_without_worker(self):
        # Exercise the deployment factory as well as injected API lifespans.
        schema_inbox = self.first
        with patch.dict(os.environ, {"AGENT_INBOX_DB_URL": self.url}), \
                patch("app.runtime.inbox.PostgresInbox", return_value=schema_inbox):
            with TestClient(create_app(service_token="test-inbound-token")) as client:
                self.assertIsNone(client.app.state.runtime)
                self.assertTrue(client.get("/health").json()["readyForTasks"])
                self.assertEqual(client.post(self.path + "/run", json=self.body,
                                             headers=AUTH).status_code, 202)
