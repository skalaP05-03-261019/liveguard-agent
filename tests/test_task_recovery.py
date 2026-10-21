"""Crash reconciliation, not replay. BE/model are controlled unless DB is specified."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from app.clients.business_mcp import AgentToolError
from app.harness.checkpoint import open_checkpointer
from app.harness.resume import resume_review
from app.integration.workflow import build_moderation_graph
from app.runtime.recovery import reconcile_task
from app.runtime.runner import TaskRunner
from app.runtime.store import MaintenanceBusy, TaskStore
import test_workflow_integration as workflow
from test_workflow_integration import REVIEW, DECISION, KICK, USER


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = f"{self.directory.name}/tasks.db"
        await workflow.WorkflowTests.prepare(self)
        self.store = TaskStore(self.path)
        self.store.accept(self.task)

    def decision(self):
        return self.task.model_copy(update={"kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
                    "review_id": UUID(REVIEW), "decision_id": UUID(DECISION)})

    async def test_crash_after_checkpoint_wait_restores_wait_then_resumes_once(self):
        self.store.claim()
        await self.graph.ainvoke({"task": self.task}, self.config)
        calls = list(self.backend.calls)
        reopened = TaskStore(self.path)
        report = await reconcile_task(reopened, self.graph, self.task.task_id)
        self.assertEqual(report["proposedStatus"], "WAITING_REVIEW")
        self.assertFalse(report["applied"])
        self.assertEqual(reopened.get(self.task.task_id)["status"], "RUNNING")
        report = await reconcile_task(reopened, self.graph, self.task.task_id, apply=True)
        self.assertTrue(report["applied"])
        self.assertEqual(self.backend.calls, calls)
        reopened.accept(self.decision())
        await TaskRunner(reopened, self.graph).run_one()
        self.assertEqual(reopened.get(self.task.task_id)["status"], "SUCCEEDED")
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_crash_after_human_decision_terminal_restores_without_apply_again(self):
        await TaskRunner(self.store, self.graph).run_one()
        resume = self.decision()
        self.store.accept(resume)
        self.store.claim()
        await resume_review(self.graph, review_kind="CONTEXT", thread_id=resume.thread_id,
                            review_id=resume.review_id, decision_id=resume.decision_id, case_id=resume.case_id)
        calls = list(self.backend.calls)
        report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertEqual(report["proposedStatus"], "SUCCEEDED")
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")
        self.assertEqual(self.backend.calls, calls)
        self.assertFalse(await TaskRunner(self.store, self.graph).run_one())

    async def test_applied_defer_restores_wait_for_next_decision_without_auto_release(self):
        await TaskRunner(self.store, self.graph).run_one()
        resume = self.decision()
        self.backend.decision = "DEFER"
        self.store.accept(resume)
        self.store.claim()
        await resume_review(self.graph, review_kind="CONTEXT", thread_id=resume.thread_id,
                            review_id=resume.review_id, decision_id=resume.decision_id, case_id=resume.case_id)
        report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertEqual(report["proposedStatus"], "WAITING_REVIEW")
        self.assertNotIn("apply", [call[0] for call in self.backend.calls])

    async def test_context_completion_restores_separate_kick_wait_without_authorizing_kick(self):
        await TaskRunner(self.store, self.graph).run_one()
        resume = self.decision()
        self.backend.decision = "CONFIRM_VIOLATION"
        self.backend.require_kick = True
        self.store.accept(resume)
        self.store.claim()
        await resume_review(self.graph, review_kind="CONTEXT", thread_id=resume.thread_id,
                            review_id=resume.review_id, decision_id=resume.decision_id, case_id=resume.case_id)
        report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertEqual(report["proposedStatus"], "WAITING_REVIEW")
        kick = resume.model_copy(update={"review_kind": "KICK", "review_id": UUID(KICK),
                                         "decision_id": UUID(USER)})
        self.store.accept(kick)
        await TaskRunner(self.store, self.graph).run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")
        self.assertNotIn("kick", [call[0] for call in self.backend.calls])
        self.assertEqual(sum(call[0] == "apply" for call in self.backend.calls), 1)

    async def test_failed_read_checkpoint_can_restore_failure_code(self):
        async def unavailable(name, args):
            raise AgentToolError("CASE_NOT_READY", trace_id=args["traceId"])
        self.business.call = unavailable
        self.store.claim()
        await self.graph.ainvoke({"task": self.task}, self.config)
        report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertEqual((report["status"], report["code"]), ("FAILED", "CASE_NOT_READY"))
        self.assertEqual(self.backend.calls, [])

    async def test_resume_claim_before_decision_is_not_mistaken_for_completed_wait(self):
        await TaskRunner(self.store, self.graph).run_one()
        self.store.accept(self.decision())
        self.store.claim()
        calls = list(self.backend.calls)
        report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertEqual(report["proposedStatus"], "UNKNOWN")
        self.assertEqual(report["recoveryCode"], "CHECKPOINT_BINDING_UNVERIFIED")
        self.assertEqual(self.backend.calls, calls)
        self.assertFalse(await TaskRunner(self.store, self.graph).run_one())

    async def test_unconfirmed_write_checkpoint_never_replays_mutation(self):
        runner = TaskRunner(self.store, self.graph)
        await runner.run_one()
        self.store.accept(self.decision())
        with patch.object(self.backend, "apply_context", side_effect=RuntimeError("secret credential chat")) as call:
            await runner.run_one()
            self.assertEqual(call.await_count, 1)
            report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
            self.assertEqual(report["proposedStatus"], "UNKNOWN")
            self.assertEqual(call.await_count, 1)
        self.assertNotIn("secret", json.dumps(report))

    async def test_wrong_checkpoint_scope_and_missing_checkpoint_stay_unknown(self):
        self.store.claim()
        report = await reconcile_task(self.store, self.graph, self.task.task_id)
        self.assertEqual(report["recoveryCode"], "CHECKPOINT_NOT_FOUND")
        await self.graph.ainvoke({"task": self.task}, self.config)
        await self.graph.aupdate_state(self.config, {"case_id": str(uuid4())})
        report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertEqual(report["recoveryCode"], "CHECKPOINT_BINDING_UNVERIFIED")
        self.assertEqual(report["proposedStatus"], "UNKNOWN")

    async def test_worker_and_recovery_exclude_each_other(self):
        with self.store.execution_lock():
            with self.assertRaises(MaintenanceBusy):
                await reconcile_task(TaskStore(self.path), self.graph, self.task.task_id, apply=True)
        with self.store.execution_lock(maintenance=True):
            self.assertFalse(await TaskRunner(TaskStore(self.path), self.graph).run_one())
        self.assertEqual(self.store.get(self.task.task_id)["status"], "QUEUED")

    async def test_process_death_releases_lock_but_does_not_requeue_claimed_job(self):
        code = """import os,sys
from app.runtime.store import TaskStore
s=TaskStore(sys.argv[1])
with s.execution_lock():
    s.claim()
    os._exit(9)
"""
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, self.path],
                                         capture_output=True, text=True)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertEqual(self.store.get(self.task.task_id)["status"], "RUNNING")
        report = await reconcile_task(TaskStore(self.path), self.graph, self.task.task_id, apply=True)
        self.assertEqual(report["proposedStatus"], "UNKNOWN")
        self.assertFalse(await TaskRunner(self.store, self.graph).run_one())
        self.assertEqual(self.backend.calls, [])

    async def test_real_shaped_backend_error_is_retained_in_public_task_status(self):
        async def unavailable(name, args):
            raise AgentToolError("CASE_NOT_READY", trace_id=args["traceId"])
        self.business.call = unavailable
        await TaskRunner(self.store, self.graph).run_one()
        record = self.store.get(self.task.task_id)
        self.assertEqual((record["status"], record["code"]), ("FAILED", "CASE_NOT_READY"))
        self.assertEqual(self.backend.calls, [])

    async def test_non_stranded_status_is_not_modified_and_checkpoint_failure_is_safe(self):
        report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertFalse(report["applied"])
        self.store.claim()
        with patch.object(self.graph, "aget_state", side_effect=RuntimeError("secret DSN")):
            report = await reconcile_task(self.store, self.graph, self.task.task_id, apply=True)
        self.assertEqual(report["recoveryCode"], "CHECKPOINT_UNAVAILABLE")
        self.assertEqual(self.store.get(self.task.task_id)["status"], "RUNNING")
        self.assertNotIn("secret", json.dumps(report))


@unittest.skipUnless(os.getenv("AGENT_TEST_CHECKPOINT_DB_URL"), "Postgres checkpoint DB required")
class PostgresRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_connections_reopen_read_only_and_recover_waiting_without_gateway(self):
        import psycopg
        from psycopg import sql
        from test_investigation import Policies
        await workflow.WorkflowTests.prepare(self)
        db_url = os.environ["AGENT_TEST_CHECKPOINT_DB_URL"]
        schema = f"agent_recovery_{uuid4().hex[:8]}"
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = f"{directory}/inbox.db"
                store = TaskStore(path)
                store.accept(self.task)
                store.claim()
                async with open_checkpointer(db_url, schema=schema) as saver:
                    graph = build_moderation_graph(model=self.model, business=self.business, policy=Policies(),
                                                   backend=self.backend, checkpointer=saver)
                    await graph.ainvoke({"task": self.task}, self.config)
                calls = list(self.backend.calls)
                async with open_checkpointer(db_url, schema=schema, initialize=False) as saver:
                    graph = build_moderation_graph(model=None, business=None, policy=None,
                                                   backend=None, checkpointer=saver)
                    report = await reconcile_task(TaskStore(path), graph, self.task.task_id, apply=True)
                self.assertEqual(report["proposedStatus"], "WAITING_REVIEW")
                self.assertEqual(self.backend.calls, calls)
        finally:
            with psycopg.connect(db_url, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))
