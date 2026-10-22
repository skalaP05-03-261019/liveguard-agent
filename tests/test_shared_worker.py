"""Real PostgreSQL competition, recovery and checkpoint fencing; no live mutations."""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from langgraph.pregel._io import map_command
from langgraph.types import Command
from psycopg import sql

from app.harness.checkpoint import open_checkpointer
from app.harness.resume import resume_review
from app.clients.business_mcp import AgentToolError
from app.integration.workflow import build_moderation_graph
from app.main import create_app
from app.runtime.fencing import GuardedGateway, LeaseGuard, current_lease
from app.runtime.fencing import FencedPostgresSaver
from app.runtime.inbox import LeaseLost, PostgresInbox
from app.runtime.worker import GraphWorker, WorkerSettings
from app.runtime.store import TaskConflict
from test_investigation import Policies
import test_workflow_integration as workflow
from test_workflow_integration import REVIEW, DECISION, KICK, USER


URL = os.getenv("AGENT_TEST_CHECKPOINT_DB_URL")


class SettingsTests(unittest.TestCase):
    def test_rejects_invalid_lease_settings(self):
        for values in ({"worker_id": ""}, {"worker_id": "x", "lease_seconds": 1},
                       {"worker_id": "x", "poll_seconds": float("nan")},
                       {"worker_id": "x", "max_attempts": 0}):
            with self.assertRaises(ValueError):
                WorkerSettings(**values)

    def test_entrypoint_misconfiguration_exits_safely(self):
        env = dict(os.environ)
        env["AGENT_INBOX_DB_URL"] = ""
        result = subprocess.run([sys.executable, "-m", "app.worker"], env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WORKER_READY", result.stdout)
        self.assertEqual(result.stderr.strip(), "WORKER_NOT_READY: WORKER_RUNTIME_UNAVAILABLE")


@unittest.skipUnless(URL, "Postgres checkpoint DB required")
class SharedWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "worker_test_" + uuid4().hex
        self.checkpoint_schema = self.schema + "_cp"
        self.store = PostgresInbox(URL, schema=self.schema)
        await asyncio.to_thread(self.store.setup)
        self.stack = AsyncExitStack()
        self.addAsyncCleanup(self.cleanup)
        await workflow.WorkflowTests.prepare(self)
        self.saver = await self.stack.enter_async_context(open_checkpointer(
            URL, schema=self.checkpoint_schema, saver_type=FencedPostgresSaver))
        self.graph = self.build(self.saver)
        self.settings = WorkerSettings(worker_id="worker-a", lease_seconds=2,
            heartbeat_seconds=0.1, poll_seconds=0.01, shutdown_seconds=0.1)
        self.store.accept(self.task)
        self.observations = []

        async def read_case(**kwargs):
            self.observations.append(("case", kwargs))
            return self.backend.refs()

        async def read_action(**kwargs):
            self.observations.append(("action", kwargs))
            return {"actionId": kwargs["action_id"], "status": "APPLIED"}

        self.backend.read_case = read_case
        self.backend.read_action = read_action

    async def cleanup(self):
        await self.stack.aclose()
        with self.store.connection() as db:
            for schema in (self.schema, self.checkpoint_schema):
                db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    def build(self, saver):
        return build_moderation_graph(model=self.model, business=self.business,
            policy=Policies(), backend=self.backend, checkpointer=saver, observer=lambda *_: None)

    def worker(self, worker_id="worker-a"):
        return GraphWorker(PostgresInbox(URL, schema=self.schema), self.graph,
            gateway=self.backend, settings=replace(self.settings, worker_id=worker_id))

    async def independent_worker(self, worker_id="worker-b", fixture=None):
        saver = await self.stack.enter_async_context(open_checkpointer(URL,
            schema=self.checkpoint_schema, initialize=False, saver_type=FencedPostgresSaver))
        if fixture is None:
            graph, backend = self.build(saver), self.backend
        else:
            backend = fixture.backend
            graph = build_moderation_graph(model=fixture.model, business=fixture.business,
                policy=Policies(), backend=backend, checkpointer=saver, observer=lambda *_: None)
        return GraphWorker(PostgresInbox(URL, schema=self.schema), graph, gateway=backend,
            settings=replace(self.settings, worker_id=worker_id))

    async def another_thread(self, route="CONTEXT_REVIEW"):
        fixture = SimpleNamespace()
        await workflow.WorkflowTests.prepare(fixture, route=route)
        fixture.task = fixture.task.model_copy(update={
            "task_id": uuid4(), "thread_id": "another-" + uuid4().hex})
        fixture.state.update(task=fixture.task, task_id=str(fixture.task.task_id),
                             thread_id=fixture.task.thread_id)
        self.store.accept(fixture.task)
        return fixture

    def row(self):
        with self.store.connection() as db:
            return db.execute(sql.SQL("SELECT * FROM {} ORDER BY sequence DESC LIMIT 1").format(
                self.store._jobs)).fetchone()

    def expire(self):
        with self.store.connection() as db:
            db.execute(sql.SQL("""
                UPDATE {} SET lease_expires_at=clock_timestamp()-interval '1 second'
                WHERE status='RUNNING'
            """).format(self.store._jobs))

    def decision(self):
        return self.task.model_copy(update={"kind": "RESUME_REVIEW", "review_kind": "CONTEXT",
            "review_id": UUID(REVIEW), "decision_id": UUID(DECISION)})

    async def invoke_owned(self, job, operation):
        guard = LeaseGuard(self.store, job)
        token = current_lease.set(guard)
        try:
            return await operation()
        finally:
            current_lease.reset(token)

    async def test_two_workers_only_one_executes_and_duplicates_preserve_receipt(self):
        one, two = self.worker(), self.worker("worker-b")
        self.assertEqual(sorted(await asyncio.gather(one.run_one(), two.run_one())), [False, True])
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual([x[0] for x in self.backend.calls], ["create"])
        receipt, created = self.store.accept(self.task.model_copy(update={"trace_id": uuid4()}))
        self.assertFalse(created)
        self.assertEqual(receipt["operationId"], "run:" + str(self.task.task_id))
        self.assertEqual(set(receipt), {"taskId", "threadId", "operationId", "status", "code"})

    async def test_atomic_claim_competition_and_stale_owner_cannot_write(self):
        claims = await asyncio.gather(*(
            asyncio.to_thread(PostgresInbox(URL, schema=self.schema).claim, f"worker-{i}")
            for i in range(12)))
        first, = [job for job in claims if job]
        self.expire()
        second = self.store.claim("replacement")
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        for operation in (
            lambda: self.store.finish(first, "SUCCEEDED"),
            lambda: self.store.heartbeat(first, lease_seconds=30),
            lambda: self.store.mark_started(first),
            lambda: self.store.mark_mutation(first, str(uuid4())),
            lambda: self.store.retry_unstarted(first),
        ):
            with self.assertRaises(LeaseLost):
                operation()
        self.store.finish(second, "FAILED", "SAFE_CODE")
        self.assertEqual(self.store.get(self.task.task_id)["status"], "FAILED")

    async def test_concurrent_new_intake_persists_one_operation_across_instances(self):
        task = self.task.model_copy(update={"task_id": uuid4(), "thread_id": "thread-" + uuid4().hex})
        receipts = await asyncio.gather(*(
            asyncio.to_thread(PostgresInbox(URL, schema=self.schema).accept, task)
            for _ in range(8)))
        self.assertEqual(sum(created for _, created in receipts), 1)
        self.assertEqual({row["operationId"] for row, _ in receipts}, {"run:" + str(task.task_id)})
        fresh = PostgresInbox(URL, schema=self.schema)
        self.assertEqual(fresh.get(task.task_id)["status"], "QUEUED")

    async def test_process_crash_unstarted_claim_is_recovered(self):
        code = """
import os,sys
from app.runtime.inbox import PostgresInbox
store=PostgresInbox(os.environ['AGENT_TEST_CHECKPOINT_DB_URL'],schema=sys.argv[1])
assert store.claim('crashed-worker',lease_seconds=30)
os._exit(9)
"""
        process = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, self.schema],
            capture_output=True, check=False)
        self.assertEqual(process.returncode, 9)
        self.expire()
        self.assertTrue(await self.worker().run_one())
        self.assertEqual(self.row()["attempts"], 2)
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")

    async def test_heartbeat_keeps_long_model_owned_without_open_transaction(self):
        entered, released = asyncio.Event(), asyncio.Event()
        original = self.model.complete

        async def slow(*args, **kwargs):
            entered.set()
            await released.wait()
            return await original(*args, **kwargs)

        self.model.complete = slow
        self.settings = replace(self.settings, lease_seconds=0.6)
        running = asyncio.create_task(self.worker().run_one())
        await asyncio.wait_for(entered.wait(), 5)
        try:
            await asyncio.sleep(0.9)
            self.assertIsNone(await asyncio.to_thread(self.store.claim, "competitor"))
            with self.store.connection() as db:
                transactions = db.execute("""
                    SELECT count(*) AS n FROM pg_stat_activity
                    WHERE state='idle in transaction' AND datname=current_database()
                """).fetchone()["n"]
            self.assertEqual(transactions, 0)
        finally:
            released.set()
            await running
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")

    async def test_lost_heartbeat_cancels_model_and_no_followup_mutation_or_write(self):
        entered = asyncio.Event()
        original = self.model.complete

        async def late(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return await original(*args, **kwargs)

        self.model.complete = late
        worker = self.worker()
        original_heartbeat = worker.store.heartbeat
        original_call = self.business.call

        async def slow_initial_read(name, arguments):
            if name == 'get_moderation_case':
                # Exercise at least one healthy heartbeat before model entry.
                await asyncio.sleep(.25)
            return await original_call(name, arguments)

        def lose_only_during_model(job, *, lease_seconds):
            # Inject the failure at the scenario boundary, not at a wall-clock
            # tick that can cancel startup on a slower CI runner.
            if entered.is_set():
                raise LeaseLost()
            return original_heartbeat(job, lease_seconds=lease_seconds)

        self.business.call = slow_initial_read
        with patch.object(worker.store, "heartbeat", side_effect=lose_only_during_model):
            running = asyncio.create_task(worker.run_one())
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(running, 5)
        snapshot = await self.graph.aget_state(self.config)
        self.assertNotIn("message_findings", snapshot.values)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.row()["status"], "RUNNING")

    async def test_stale_checkpoint_and_pending_writes_are_transactionally_fenced(self):
        first = self.store.claim("old")
        self.store.mark_started(first)
        await self.invoke_owned(first, lambda: self.graph.ainvoke({"task": self.task}, self.config))
        snapshot = await self.graph.aget_state(self.config)
        self.expire()
        second = self.store.claim("new")
        replacement = await self.independent_worker()
        await self.invoke_owned(second, lambda: replacement.graph.aupdate_state(
            self.config, {"backend_result": {"owner": "new"}}))
        owned_snapshot = await replacement.graph.aget_state(self.config)
        async def stale_writes():
            with self.assertRaises(LeaseLost):
                await self.graph.aupdate_state(self.config, {"task_outcome": "SUCCEEDED"})
            with self.assertRaises(LeaseLost):
                await self.saver.aput_writes(snapshot.config, [("task_outcome", "SUCCEEDED")], "late")
        await self.invoke_owned(first, stale_writes)
        state = await self.graph.aget_state(self.config)
        self.assertEqual(state.config, owned_snapshot.config)
        self.assertEqual(state.values["backend_result"], {"owner": "new"})
        self.assertNotIn("task_outcome", state.values)
        self.store.finish(second, "WAITING_REVIEW")

    async def test_checkpoint_transaction_rolls_back_when_lease_expires_before_commit(self):
        job = self.store.claim("old", lease_seconds=0.15)
        async def checkpoint_write():
            with self.assertRaises(LeaseLost):
                async with self.saver._cursor(pipeline=True) as cur:
                    await cur.execute(sql.SQL("UPDATE {} SET code='STALE'").format(self.store._jobs))
                    await asyncio.sleep(0.2)
        await self.invoke_owned(job, checkpoint_write)
        self.assertIsNone(self.row()["code"])
        self.assertIsNotNone(self.store.claim("new"))

    async def test_crash_before_readonly_pending_node_continues_checkpoint_not_initial_task(self):
        job = self.store.claim("dead")
        self.store.mark_started(job)
        self.graph.interrupt_before_nodes = ["investigate"]
        await self.invoke_owned(job, lambda: self.graph.ainvoke({"task": self.task}, self.config))
        snapshot = await self.graph.aget_state(self.config)
        self.assertEqual(snapshot.next, ("investigate",))
        loaded = self.business.calls.count("get_moderation_case")
        self.graph.interrupt_before_nodes = []
        self.expire()
        await self.worker().run_one()
        self.assertEqual(self.business.calls.count("get_moderation_case"), loaded)
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")

    async def test_expired_completed_wait_checkpoint_restored_without_model_or_create(self):
        job = self.store.claim("dead")
        self.store.mark_started(job)
        await self.invoke_owned(job, lambda: self.graph.ainvoke({"task": self.task}, self.config))
        calls, model_calls = list(self.backend.calls), len(self.model.inputs)
        self.expire()
        await self.worker().run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual(self.backend.calls, calls)
        self.assertEqual(len(self.model.inputs), model_calls)

    async def test_expired_resume_terminal_restored_without_apply_again(self):
        await self.worker().run_one()
        task = self.decision()
        self.store.accept(task)
        job = self.store.claim("dead")
        self.store.mark_started(job)
        await self.invoke_owned(job, lambda: resume_review(self.graph,
            review_kind=task.review_kind, thread_id=task.thread_id,
            review_id=task.review_id, decision_id=task.decision_id, case_id=task.case_id))
        self.expire()
        await self.worker().run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")
        self.assertEqual(sum(x[0] == "apply" for x in self.backend.calls), 1)

    async def test_independent_worker_connections_resume_same_checkpoint_once(self):
        await self.worker().run_one()
        self.store.accept(self.decision())
        saver = await self.stack.enter_async_context(open_checkpointer(URL,
            schema=self.checkpoint_schema, initialize=False, saver_type=FencedPostgresSaver))
        second = GraphWorker(PostgresInbox(URL, schema=self.schema), self.build(saver),
            gateway=self.backend, settings=replace(self.settings, worker_id="worker-b"))
        self.assertIsNot(saver.conn, self.saver.conn)
        self.assertTrue(await second.run_one())
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")
        self.assertEqual(sum(x[0] == "create" for x in self.backend.calls), 1)
        self.assertEqual(sum(x[0] == "apply" for x in self.backend.calls), 1)
        receipt, created = self.store.accept(self.decision())
        self.assertFalse(created)
        self.assertEqual(receipt["status"], "SUCCEEDED")
        self.assertFalse(await self.worker().run_one())

    async def test_early_resume_waits_for_active_thread_without_blocking_other_thread(self):
        entered, released = asyncio.Event(), asyncio.Event()
        original = self.backend.create_review

        async def slow(**kwargs):
            result = await original(**kwargs)
            entered.set()  # BE has committed; no interrupt checkpoint exists yet.
            await released.wait()
            return result

        self.backend.create_review = slow
        first = asyncio.create_task(self.worker().run_one())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            receipt, created = self.store.accept(self.decision())
            self.assertTrue(created)
            self.assertEqual(receipt["status"], "QUEUED")
            second = await self.independent_worker()
            self.assertFalse(await second.run_one())
            other = await self.another_thread(route="PASS")
            other_worker = await self.independent_worker("worker-c", other)
            self.assertTrue(await other_worker.run_one())
            self.assertEqual(self.store.get(other.task.task_id)["status"], "SUCCEEDED")
            self.assertFalse(first.done())
        finally:
            released.set()
            await asyncio.wait_for(first, 5)
        self.assertTrue(await second.run_one())
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_distinct_threads_execute_truly_concurrently_with_independent_savers(self):
        other = await self.another_thread()
        entered_a, entered_b, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        def block(model, entered):
            original = model.complete

            async def complete(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)
            model.complete = complete

        block(self.model, entered_a)
        block(other.model, entered_b)
        workers = {
            self.task.thread_id: self.worker("a"),
            other.task.thread_id: await self.independent_worker("b", other),
        }
        # Start in claim order so each fixture handles its own thread. The
        # second worker must enter while the first still holds an active lease.
        threads = sorted(workers)
        events = {self.task.thread_id: entered_a, other.task.thread_id: entered_b}
        running = [asyncio.create_task(workers[threads[0]].run_one())]
        try:
            await asyncio.wait_for(events[threads[0]].wait(), 5)
            running.append(asyncio.create_task(workers[threads[1]].run_one()))
            await asyncio.wait_for(asyncio.gather(entered_a.wait(), entered_b.wait()), 5)
            self.assertTrue(all(not task.done() for task in running))
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(*running), 5)
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")
        self.assertEqual(self.store.get(other.task.task_id)["status"], "WAITING_REVIEW")

    async def test_wait_releases_workers_slot_and_thread_claim(self):
        worker = self.worker()
        await worker.run_one()
        waiting = self.row()
        self.assertIsNone(waiting["lease_token"])
        self.assertIsNone(waiting["worker_id"])
        self.assertIsNone(waiting["lease_expires_at"])
        other = await self.another_thread(route="PASS")
        # Same worker process has no held approval slot; a new execution starts.
        self.business.fixture["case"]["handlingRoute"] = "PASS"
        try:
            self.assertTrue(await worker.run_one())
        finally:
            self.business.fixture["case"]["handlingRoute"] = "CONTEXT_REVIEW"
        self.assertEqual(self.store.get(other.task.task_id)["status"], "SUCCEEDED")
        self.store.accept(self.decision())
        second = await self.independent_worker()
        self.assertTrue(await second.run_one())
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")

    async def test_early_resume_survives_lost_waiting_inbox_update_and_replacement(self):
        job = self.store.claim("dead")
        self.store.mark_started(job)
        await self.invoke_owned(job, lambda: self.graph.ainvoke({"task": self.task}, self.config))
        before = await self.graph.aget_state(self.config)
        calls = len(self.model.inputs)
        self.assertTrue(before.interrupts)
        self.assertEqual(self.row()["status"], "RUNNING")
        self.store.accept(self.decision())
        replacement = await self.independent_worker()
        self.assertFalse(await replacement.run_one())
        self.expire()
        self.assertTrue(await replacement.run_one())  # Recover run, not resume.
        self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)
        self.assertEqual(self.row()["status"], "QUEUED")
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "SUCCEEDED")
        self.assertEqual(len(self.model.inputs), calls)
        self.assertEqual(sum(c[0] == "create" for c in self.backend.calls), 1)
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_resume_crash_before_command_is_revalidated_and_recovered(self):
        await self.worker().run_one()
        self.store.accept(self.decision())
        job = self.store.claim("dead")
        self.store.mark_started(job)
        self.expire()
        replacement = await self.independent_worker()
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "SUCCEEDED")
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_transient_recovery_decision_read_retries_without_resetting_started_state(self):
        await self.worker().run_one()
        task = self.decision()
        self.store.accept(task)
        job = self.store.claim("dead")
        self.store.mark_started(job)
        before = await self.graph.aget_state(self.config)
        original = self.backend.get_decision
        calls = 0

        async def transient_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AgentToolError("BACKEND_DELIVERY_UNKNOWN", trace_id=str(task.trace_id))
            return await original(**kwargs)

        self.backend.get_decision = transient_once
        self.expire()
        replacement = await self.independent_worker()
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "QUEUED")
        self.assertTrue(self.row()["started"])
        self.assertFalse(self.row()["mutation_started"])
        self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET available_at=clock_timestamp()").format(self.store._jobs))
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "SUCCEEDED")
        self.assertEqual(calls, 2)

    async def test_transient_recovery_decision_read_stops_after_max_attempts(self):
        await self.worker().run_one()
        task = self.decision()
        self.store.accept(task)
        job = self.store.claim("dead")
        self.store.mark_started(job)
        before = await self.graph.aget_state(self.config)

        async def unavailable(**kwargs):
            raise AgentToolError("BACKEND_DELIVERY_UNKNOWN", trace_id=str(task.trace_id))

        self.backend.get_decision = unavailable
        self.expire()
        replacement = await self.independent_worker()
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "QUEUED")
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET available_at=clock_timestamp()").format(self.store._jobs))
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "UNKNOWN")
        self.assertEqual(self.row()["code"], "DECISION_REVALIDATION_UNAVAILABLE")
        self.assertTrue(self.row()["started"])
        self.assertFalse(self.row()["mutation_started"])
        self.assertEqual(self.row()["attempts"], self.settings.max_attempts)
        self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)

    async def test_resume_pending_readonly_checkpoint_continues_without_redelivery(self):
        await self.worker().run_one()
        task = self.decision()
        self.store.accept(task)
        job = self.store.claim("dead")
        self.store.mark_started(job)
        self.graph.interrupt_before_nodes = ["apply_context_decision"]
        await self.invoke_owned(job, lambda: resume_review(self.graph,
            review_kind=task.review_kind, thread_id=task.thread_id,
            review_id=task.review_id, decision_id=task.decision_id, case_id=task.case_id))
        self.assertEqual((await self.graph.aget_state(self.config)).next, ("apply_context_decision",))
        self.expire()
        original = self.backend.get_decision
        calls = 0

        async def transient_once(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AgentToolError("BACKEND_DELIVERY_UNKNOWN", trace_id=str(task.trace_id))
            return await original(**kwargs)

        self.backend.get_decision = transient_once
        replacement = await self.independent_worker()
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "QUEUED")
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET available_at=clock_timestamp()").format(self.store._jobs))
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "SUCCEEDED")
        self.assertEqual(calls, 2)
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_defer_then_final_queued_decision_and_duplicates_survive_replacement(self):
        await self.worker().run_one()
        original = self.backend.get_decision
        final = self.decision().model_copy(update={"decision_id": uuid4()})

        async def stored(**kwargs):
            result = await original(**kwargs)
            result["decision"] = "DEFER" if kwargs["decision_id"] == DECISION else "CONFIRM_NORMAL"
            return result

        self.backend.get_decision = stored
        self.store.accept(self.decision())
        self.store.accept(final)
        self.assertTrue(await self.worker().run_one())
        snapshot = await self.graph.aget_state(self.config)
        self.assertTrue(snapshot.interrupts)
        self.assertEqual(snapshot.values["context_result"]["decision"], "DEFER")
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 0)
        replacement = await self.independent_worker()
        self.assertTrue(await replacement.run_one())
        self.assertEqual(self.row()["status"], "SUCCEEDED")
        receipt, created = self.store.accept(self.decision())
        self.assertFalse(created)
        self.assertEqual(receipt["status"], "WAITING_REVIEW")
        receipt, created = self.store.accept(final)
        self.assertFalse(created)
        self.assertEqual(receipt["status"], "SUCCEEDED")
        self.assertFalse(await replacement.run_one())
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_durable_resume_writes_recover_defer_without_losing_next_decision(self):
        await self.worker().run_one()
        original = self.backend.get_decision
        final = self.decision().model_copy(update={"decision_id": uuid4()})

        async def stored(**kwargs):
            result = await original(**kwargs)
            result["decision"] = "DEFER" if kwargs["decision_id"] == DECISION else "CONFIRM_NORMAL"
            return result

        self.backend.get_decision = stored
        self.store.accept(self.decision())
        job = self.store.claim("dead")
        self.store.mark_started(job)
        snapshot = await self.graph.aget_state(self.config)
        command = Command(resume={"reviewId": REVIEW, "decisionId": DECISION})
        # Process death after Command writes commit but before the waiting node
        # consumes them. Use the installed LangGraph's actual write encoding.
        async def persist_command():
            for task_id, channel, value in map_command(cmd=command):
                await self.saver.aput_writes(snapshot.config, [(channel, value)], task_id)
        await self.invoke_owned(job, persist_command)
        self.store.accept(final)
        self.expire()
        replacement = await self.independent_worker()
        await replacement.run_one()
        state = await self.graph.aget_state(self.config)
        self.assertTrue(state.interrupts)
        self.assertEqual(state.values["context_result"]["decision"], "DEFER")
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 0)
        self.assertEqual(self.row()["status"], "QUEUED")
        await replacement.run_one()
        self.assertEqual(self.row()["status"], "SUCCEEDED")
        self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 1)

    async def test_kick_resume_reads_persisted_kind_and_decision_before_revalidation(self):
        self.backend.require_kick = True
        await self.worker().run_one()
        self.store.accept(self.decision())
        await self.worker().run_one()
        self.assertEqual(self.row()["status"], "WAITING_REVIEW")
        original = self.backend.get_decision
        task = self.decision().model_copy(update={
            "review_kind": "KICK", "review_id": UUID(workflow.KICK), "decision_id": uuid4()})
        self.store.accept(task)
        await self.worker().run_one()  # CONTEXT value cannot authorize KICK.
        self.assertEqual(self.row()["code"], "DECISION_BINDING_MISMATCH")
        self.assertNotIn("revalidate", [call[0] for call in self.backend.calls])

        async def rejected(**kwargs):
            result = await original(**kwargs)
            result["decision"] = "REJECT"
            return result

        self.backend.get_decision = rejected
        self.store.accept(task.model_copy(update={"decision_id": uuid4()}))
        replacement = await self.independent_worker()
        await replacement.run_one()
        self.assertEqual(self.row()["status"], "SUCCEEDED")
        self.assertEqual(sum(c[0] == "revalidate" for c in self.backend.calls), 1)
        self.assertNotIn("kick", [call[0] for call in self.backend.calls])

    async def test_invalid_persisted_decisions_rejected_before_checkpoint_or_effects(self):
        await self.worker().run_one()
        before = await self.graph.aget_state(self.config)
        original = self.backend.get_decision
        for key, value in (
            ("reviewId", str(uuid4())), ("decisionId", str(uuid4())),
            ("caseId", str(uuid4())), ("liveId", str(uuid4())),
            ("snapshotId", str(uuid4())), ("evidenceVersion", 99),
            ("evidenceVersion", True),
            ("policyVersion", "wrong"), ("threadId", "wrong"), ("kind", "KICK"),
            ("status", "PENDING"), ("decision", "APPROVE"), ("value", "DEFER"),
        ):
            with self.subTest(key=key):
                async def invalid(**kwargs):
                    return {**await original(**kwargs), key: value}
                self.backend.get_decision = invalid
                task = self.decision().model_copy(update={"decision_id": uuid4()})
                self.store.accept(task)
                await self.worker().run_one()
                self.assertEqual(self.row()["status"], "FAILED")
                self.assertEqual(self.row()["code"], "DECISION_BINDING_MISMATCH")
                self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)
                self.assertEqual(sum(c[0] == "apply" for c in self.backend.calls), 0)
        self.backend.get_decision = original
        self.store.accept(self.decision())
        await self.worker().run_one()
        self.assertEqual(self.row()["status"], "SUCCEEDED")

    async def test_wrong_review_or_kind_rejects_without_even_reading_decision(self):
        await self.worker().run_one()
        before = await self.graph.aget_state(self.config)
        for changes, code in (
            ({"review_kind": "KICK"}, "REVIEW_KIND_MISMATCH"),
            ({"review_id": uuid4()}, "REVIEW_MISMATCH"),
        ):
            task = self.decision().model_copy(update={**changes, "decision_id": uuid4()})
            self.store.accept(task)
            await self.worker().run_one()
            self.assertEqual(self.row()["status"], "FAILED")
            self.assertEqual(self.row()["code"], code)
            self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)
        self.assertEqual([c[0] for c in self.backend.calls], ["create"])

    async def test_checkpoint_task_binding_is_checked_before_resume_or_decision_read(self):
        await self.worker().run_one()
        self.store.accept(self.decision())
        job = self.store.claim("owner")
        await self.invoke_owned(job, lambda: self.graph.aupdate_state(
            self.config, {"task_id": str(uuid4())}))
        before = await self.graph.aget_state(self.config)
        await self.worker()._execute(LeaseGuard(self.store, job))
        self.assertEqual(self.row()["status"], "FAILED")
        self.assertEqual(self.row()["code"], "CHECKPOINT_BINDING_MISMATCH")
        self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)
        self.assertEqual([c[0] for c in self.backend.calls], ["create"])

    async def test_pending_recovery_cannot_execute_another_operations_decision(self):
        await self.worker().run_one()
        task = self.decision()
        self.store.accept(task)
        job = self.store.claim("dead")
        self.store.mark_started(job)
        self.graph.interrupt_before_nodes = ["apply_context_decision"]
        await self.invoke_owned(job, lambda: resume_review(self.graph,
            review_kind=task.review_kind, thread_id=task.thread_id,
            review_id=task.review_id, decision_id=task.decision_id, case_id=task.case_id))
        await self.invoke_owned(job, lambda: self.graph.aupdate_state(
            self.config, {"context_decision_id": str(uuid4())}))
        before = await self.graph.aget_state(self.config)
        self.assertEqual(before.next, ("apply_context_decision",))
        self.expire()
        replacement = await self.independent_worker()
        await replacement.run_one()
        self.assertEqual(self.row()["status"], "UNKNOWN")
        self.assertEqual(self.row()["code"], "CHECKPOINT_BINDING_UNVERIFIED")
        self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)
        self.assertEqual([c[0] for c in self.backend.calls], ["create"])

    async def test_missing_decision_explicitly_rejected_but_transient_reads_retry_bounded(self):
        await self.worker().run_one()

        async def missing(**kwargs):
            raise AgentToolError("NOT_FOUND", trace_id=str(self.task.trace_id))

        self.backend.get_decision = missing
        self.store.accept(self.decision())
        await self.worker().run_one()
        self.assertEqual(self.row()["code"], "DECISION_NOT_FOUND")
        before = await self.graph.aget_state(self.config)

        async def unavailable(**kwargs):
            raise AgentToolError("BACKEND_DELIVERY_UNKNOWN", trace_id=str(self.task.trace_id))

        self.backend.get_decision = unavailable
        self.store.accept(self.decision().model_copy(update={"decision_id": uuid4()}))
        for attempt in range(3):
            await self.worker().run_one()
            self.assertFalse(self.row()["started"])
            self.assertFalse(self.row()["mutation_started"])
            with self.store.connection() as db:
                db.execute(sql.SQL("UPDATE {} SET available_at=clock_timestamp()").format(self.store._jobs))
        self.assertEqual(self.row()["status"], "FAILED")
        self.assertEqual(self.row()["code"], "ATTEMPTS_EXHAUSTED")
        self.assertEqual((await self.graph.aget_state(self.config)).config, before.config)

    async def test_pending_mutating_resume_reconciles_be_without_replay(self):
        await self.worker().run_one()
        self.store.accept(self.decision())
        async def uncertain(**kwargs):
            self.backend.calls.append(("uncertain_apply", kwargs))
            raise RuntimeError("secret argument credential text")
        self.backend.apply_context = uncertain
        await self.worker().run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "UNKNOWN")
        self.assertEqual([x[0] for x in self.observations], ["case"])
        self.assertEqual(sum(x[0] == "uncertain_apply" for x in self.backend.calls), 1)
        self.assertFalse(await self.worker("other").run_one())
        self.assertNotIn("secret", json.dumps(self.store.get(self.task.task_id)))

    async def test_unknown_action_uses_original_action_id_and_never_reissues_command(self):
        await self.worker().run_one()
        self.store.accept(self.decision())
        job = self.store.claim("dead")
        self.store.mark_started(job)
        action = str(uuid4())
        self.store.mark_mutation(job, action)
        self.expire()
        await self.worker().run_one()
        self.assertEqual(self.observations[0][1]["action_id"], action)
        self.assertEqual(self.row()["action_id"], UUID(action))
        self.assertEqual(self.store.get(self.task.task_id)["status"], "NEEDS_REVIEW")
        self.assertNotIn("kick", [x[0] for x in self.backend.calls])

    async def test_unknown_be_status_and_binding_mismatch_do_not_become_success(self):
        job = self.store.claim("dead")
        self.store.mark_started(job)
        self.store.mark_mutation(job, str(uuid4()))
        self.expire()
        async def wrong(**kwargs):
            return {"actionId": str(uuid4()), "status": "APPLIED"}
        self.backend.read_action = wrong
        await self.worker().run_one()
        self.assertEqual(self.store.get(self.task.task_id)["code"], "INVALID_BACKEND_OBSERVATION")
        self.assertEqual(self.store.get(self.task.task_id)["status"], "UNKNOWN")

    async def test_unavailable_backend_observation_stays_operator_unknown(self):
        job = self.store.claim("dead")
        self.store.mark_started(job)
        self.store.mark_mutation(job, str(uuid4()))
        self.expire()
        async def unavailable(**kwargs):
            raise RuntimeError("credentials original text")
        self.backend.read_action = unavailable
        await self.worker().run_one()
        self.assertEqual(self.store.get(self.task.task_id)["code"], "BACKEND_RECONCILIATION_UNAVAILABLE")
        self.assertEqual(self.store.get(self.task.task_id)["status"], "UNKNOWN")

    async def test_original_action_command_and_idempotency_key_are_passed_unchanged(self):
        job = self.store.claim("owner")
        command = {"actionId": str(uuid4()), "idempotencyKey": "existing-backend-key"}
        seen = []
        async def kick(action_command, **kwargs):
            seen.append(action_command)
            return {"actionId": action_command["actionId"], "status": "UNKNOWN"}
        self.backend.kick = kick
        async def invoke():
            await GuardedGateway(self.backend).kick(command, trace_id=str(self.task.trace_id))
        await self.invoke_owned(job, invoke)
        self.assertIs(seen[0], command)
        self.assertEqual(self.row()["action_id"], UUID(command["actionId"]))
        self.assertNotIn("existing-backend-key", json.dumps(self.store.get(self.task.task_id)))

    async def test_retry_backoff_and_final_attempts_are_explicit(self):
        job = self.store.claim("worker")
        self.store.retry_unstarted(job, max_attempts=2, backoff_seconds=10)
        self.assertIsNone(self.store.claim("next"))
        self.assertEqual(self.store.get(self.task.task_id)["code"], "RETRY_BACKOFF")
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET available_at=clock_timestamp()").format(self.store._jobs))
        job = self.store.claim("next")
        self.store.retry_unstarted(job, max_attempts=2)
        self.assertEqual(self.store.get(self.task.task_id)["code"], "ATTEMPTS_EXHAUSTED")
        self.assertEqual(self.store.get(self.task.task_id)["status"], "FAILED")
        self.assertIsNone(self.store.claim("third"))

    async def test_graceful_shutdown_stops_claiming_and_finishes_active_task(self):
        entered, released = asyncio.Event(), asyncio.Event()
        original = self.backend.create_review
        async def slow(**kwargs):
            entered.set()
            await released.wait()
            return await original(**kwargs)
        self.backend.create_review = slow
        worker = self.worker()
        worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(worker.close())
        await asyncio.sleep(0)
        self.assertFalse(worker.ready)
        self.assertFalse(await worker.run_one())
        released.set()
        await closing
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")

    async def test_shutdown_timeout_leaves_uncertain_effect_for_expiry_and_be_read(self):
        entered = asyncio.Event()
        async def inflight(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.backend.create_review = inflight
        worker = self.worker()
        worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        await worker.close()
        # app.worker.main and configured_worker.finally both close the same worker.
        await worker.close()
        self.assertEqual(self.row()["status"], "RUNNING")
        self.assertTrue(self.row()["mutation_started"])
        self.expire()
        await self.worker("replacement").run_one()
        self.assertEqual(self.store.get(self.task.task_id)["status"], "UNKNOWN")
        self.assertEqual([x[0] for x in self.observations], ["case"])

    async def test_shutdown_fences_cancellation_resistant_dependency(self):
        entered, cancelled, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
        followup_blocked = asyncio.Event()
        gateway = GuardedGateway(self.backend)

        async def inflight(**kwargs):
            entered.set()
            try:
                await released.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await released.wait()
            with self.assertRaises(LeaseLost):
                await gateway.kick({"actionId": str(uuid4())},
                                   trace_id=str(self.task.trace_id))
            followup_blocked.set()
            raise LeaseLost()

        self.backend.create_review = inflight
        worker = self.worker()
        worker.start()
        try:
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.wait_for(worker.close(), 1)
            self.assertTrue(cancelled.is_set())
            self.assertTrue(worker.active_guard.lost)
            self.assertTrue(worker.active_heartbeat.done())
            self.assertEqual(self.row()["status"], "RUNNING")
        finally:
            released.set()
            await asyncio.wait_for(asyncio.gather(worker.worker, return_exceptions=True), 5)
        self.assertTrue(followup_blocked.is_set())
        self.assertNotIn("kick", [call[0] for call in self.backend.calls])
        self.assertEqual(self.row()["status"], "RUNNING")

    async def test_boundary_checks_between_revalidation_and_kick(self):
        job = self.store.claim("old")
        gateway = GuardedGateway(self.backend)
        async def revalidate(**kwargs):
            self.expire()
            return {"status": "APPROVED", "actionCommand": {"actionId": str(uuid4())}}
        self.backend.revalidate = revalidate
        async def invoke():
            with self.assertRaises(LeaseLost):
                await gateway.revalidate(review_id=REVIEW, decision_id=DECISION,
                                         trace_id=str(self.task.trace_id))
            with self.assertRaises(LeaseLost):
                await gateway.kick({"actionId": str(uuid4())}, trace_id=str(self.task.trace_id))
        await self.invoke_owned(job, invoke)
        self.assertEqual(self.backend.calls, [])

    async def test_api_receipts_and_shared_status_remain_api_only(self):
        @asynccontextmanager
        async def inbox_factory():
            yield self.store
        @asynccontextmanager
        async def forbidden_runtime():
            raise AssertionError("API must not compose a Graph")
            yield
        app = create_app(service_token="inbox-test", inbox_factory=inbox_factory,
                         runtime_factory=forbidden_runtime)
        def requests():
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer inbox-test"}
                response = client.post(f"/agent/tasks/{self.task.task_id}/run",
                    json=self.task.model_dump(mode="json", by_alias=True), headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["duplicate"])
                self.assertEqual(client.get("/health").json()["readyForTasks"], True)
                self.assertEqual(client.get(f"/agent/tasks/{self.task.task_id}",
                    headers=headers).json()["status"], "QUEUED")
        await asyncio.to_thread(requests)
        conflict = self.task.model_copy(update={"case_id": uuid4()})
        with self.assertRaises(TaskConflict):
            self.store.accept(conflict)

    async def test_fatal_store_failure_surfaces_worker_readiness_error(self):
        worker = self.worker()
        with patch.object(worker.store, "claim", side_effect=RuntimeError("credentials")):
            worker.start()
            with self.assertRaisesRegex(Exception, "^WORKER_RUNTIME_UNAVAILABLE$"):
                await worker.worker
        self.assertFalse(worker.ready)
        self.assertEqual(worker.failure_code, "WORKER_RUNTIME_UNAVAILABLE")

    async def test_standalone_composition_runs_existing_graph_with_fenced_saver(self):
        from app.worker import configured_worker
        from app.harness.checkpoint import CheckpointSettings
        @asynccontextmanager
        async def gateway_factory():
            yield self.backend
        def checkpointer(url, **kwargs):
            return open_checkpointer(url, schema=self.checkpoint_schema, **kwargs)
        with patch.dict(os.environ, {"AGENT_INBOX_DB_URL": URL,
                    "AGENT_GATEWAY_FACTORY": "app.worker:test_gateway_factory"}), \
                patch("app.worker.test_gateway_factory", gateway_factory, create=True), \
                patch("app.worker.PostgresInbox", return_value=self.store), \
                patch("app.worker.open_checkpointer", side_effect=checkpointer), \
                patch("app.worker.CheckpointSettings.from_env", return_value=CheckpointSettings(URL)), \
                patch("app.worker.BusinessMcpSettings.from_env"), \
                patch("app.worker.RagMcpSettings.from_env"), \
                patch("app.worker.BusinessMcpReadClient", return_value=self.business), \
                patch("app.worker.RagMcpReadClient", return_value=Policies()), \
                patch("app.worker.model_from_env", return_value=self.model):
            async with configured_worker() as worker:
                self.assertIsInstance(worker.graph.checkpointer, FencedPostgresSaver)
                self.assertTrue(await worker.run_one())
        self.assertEqual(self.store.get(self.task.task_id)["status"], "WAITING_REVIEW")

    def process_args(self, audit, *extra):
        return [sys.executable, 'tests/fixtures/worker_process.py', '--schema', self.schema,
                '--checkpoint-schema', self.checkpoint_schema, '--audit', str(audit), *extra]

    async def run_process(self, audit, *extra):
        result = await asyncio.to_thread(subprocess.run, self.process_args(audit, *extra),
                                         capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    @staticmethod
    def process_events(audit):
        return [json.loads(line) for line in audit.read_text().splitlines()]

    async def test_restart_processes_preserve_ai_reason_context_and_separate_kick_once(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory)/'controlled-events.jsonl'
            await self.run_process(audit)
            self.assertEqual(self.store.get(self.task.task_id)['status'], 'WAITING_REVIEW')
            events = self.process_events(audit)
            card = next(row['value'] for row in events if row['event'] == 'create')
            self.assertTrue(card['findings'][0]['reason'])
            self.assertTrue(card['findings'][0]['policyIds'])
            self.assertEqual(card['findings'][0]['evidenceMessageIds'],
                             [str(self.state['target_messages'][0]['messageId'])])
            self.assertEqual(card['caseId'], str(self.task.case_id))
            self.store.accept(self.decision())
            await self.run_process(audit)
            self.assertEqual(self.store.get(self.task.task_id)['status'], 'WAITING_REVIEW')
            self.assertNotIn('kick', [row['event'] for row in self.process_events(audit)])
            pending = (await self.graph.aget_state(self.config)).interrupts[0].value
            self.assertEqual(pending['reviewKind'], 'KICK')
            kick_task = self.decision().model_copy(update={
                'review_kind': 'KICK', 'review_id': UUID(KICK), 'decision_id': UUID(USER)})
            self.store.accept(kick_task)
            await self.run_process(audit)
            self.assertEqual(self.store.get(self.task.task_id)['status'], 'SUCCEEDED')
            self.assertFalse(self.store.accept(kick_task)[1])
            await self.run_process(audit)
            events = self.process_events(audit)
            self.assertEqual(sum(row['event'] == 'model' for row in events), 2)
            self.assertEqual(sum(row['event'] == 'create' for row in events), 1)
            self.assertEqual(sum(row['event'] == 'apply' for row in events), 1)
            commands = [row['value'] for row in events if row['event'] == 'kick']
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]['actionId'], DECISION)
            self.assertEqual(commands[0]['idempotencyKey'], DECISION)
            state = (await self.graph.aget_state(self.config)).values
            self.assertEqual(state['message_findings'][0].verdict, 'NO_VIOLATION')
            self.assertEqual(state['context_result']['decision'], 'CONFIRM_VIOLATION')
            self.assertEqual(state['kick_result']['decisionId'], USER)
            self.assertTrue(state['kick_result']['kicked'])

    async def test_restart_kick_rejection_never_executes_kick(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory)/'controlled-events.jsonl'
            await self.run_process(audit)
            self.store.accept(self.decision())
            await self.run_process(audit)
            task = self.decision().model_copy(update={
                'review_kind': 'KICK', 'review_id': UUID(KICK), 'decision_id': UUID(USER)})
            self.store.accept(task)
            await self.run_process(audit, '--kick-status', 'REJECTED')
            self.assertEqual(self.store.get(self.task.task_id)['status'], 'SUCCEEDED')
            state = (await self.graph.aget_state(self.config)).values
            self.assertEqual(state['kick_result']['status'], 'REJECTED')
            self.assertFalse(state['kick_result']['kicked'])
            self.assertNotIn('kick', [row['event'] for row in self.process_events(audit)])

    async def test_killed_process_during_review_mutation_is_read_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory)/'controlled-events.jsonl'
            process = subprocess.Popen(self.process_args(audit, '--block-create'),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(500):
                    if audit.exists() and any(row['event'] == 'create' for row in self.process_events(audit)):
                        break
                    if process.poll() is not None:
                        self.fail('Controlled worker exited before mutation')
                    await asyncio.sleep(.01)
                else:
                    self.fail('Controlled worker did not enter mutation')
                self.assertTrue(self.row()['mutation_started'])
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
                self.expire()
                await self.run_process(audit)
                events = self.process_events(audit)
                self.assertEqual(sum(row['event'] == 'create' for row in events), 1)
                self.assertEqual(sum(row['event'] == 'read_case' for row in events), 1)
                self.assertEqual(self.store.get(self.task.task_id)['status'], 'UNKNOWN')
                self.assertFalse(await self.worker('after-recovery').run_one())
            finally:
                if process.poll() is None:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)

    async def test_defer_and_wrong_review_keep_waiting_without_reinvestigation(self):
        await self.worker().run_one()
        wrong = self.decision().model_copy(update={'review_kind': 'KICK'})
        self.store.accept(wrong)
        await self.worker('second').run_one()
        self.assertEqual(self.store.get(self.task.task_id)['status'], 'WAITING_REVIEW')
        self.assertEqual(self.store.get(self.task.task_id)['code'], 'REVIEW_KIND_MISMATCH')
        self.backend.decision = 'DEFER'
        deferred = self.decision().model_copy(update={'decision_id': uuid4()})
        self.store.accept(deferred)
        await self.worker('third').run_one()
        self.assertEqual(self.store.get(self.task.task_id)['status'], 'WAITING_REVIEW')
        self.assertEqual(sum(row[0] == 'create' for row in self.backend.calls), 1)
        self.assertEqual(sum(row[0] == 'apply' for row in self.backend.calls), 0)
        self.assertEqual(len(self.model.inputs), 2)

    async def test_cancelling_shutdown_caller_does_not_swallow_cancellation(self):
        entered, released = asyncio.Event(), asyncio.Event()
        original = self.backend.create_review
        async def slow(**kwargs):
            entered.set()
            await released.wait()
            return await original(**kwargs)
        self.backend.create_review = slow
        worker = self.worker()
        worker.start()
        try:
            await asyncio.wait_for(entered.wait(), 5)
            closing = asyncio.create_task(worker.close())
            await asyncio.sleep(0)
            closing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closing
            self.assertFalse(worker.worker.done())
        finally:
            released.set()
            await asyncio.wait_for(worker.close(), 5)

    async def test_wrong_live_decision_persists_failure_without_apply_or_kick(self):
        await self.worker().run_one()
        self.backend.wrong_live = True
        self.store.accept(self.decision())
        await self.worker('second').run_one()
        self.assertEqual(self.store.get(self.task.task_id)['status'], 'FAILED')
        self.assertNotIn('apply', [row[0] for row in self.backend.calls])
        self.assertNotIn('kick', [row[0] for row in self.backend.calls])
