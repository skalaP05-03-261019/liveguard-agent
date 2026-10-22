"""#89 admission and quota tests use independent connections to real PostgreSQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import sql

from app.clients.business_mcp import AgentToolError
from app.harness.checkpoint import open_checkpointer
from app.harness.execution import HarnessGate
from app.integration.workflow import build_moderation_graph
from app.investigation.model import (
    ModelLimits, ModelRequest, ModelTurn, OpenAIInvestigationModel, model_from_env,
    retry_after_seconds,
)
from app.runtime.fencing import FencedPostgresSaver, LeaseGuard, current_lease
from app.runtime.inbox import LeaseLost, PostgresInbox
from app.runtime.store import InboxCapacityExceeded, task_payload
from app.runtime.worker import GraphWorker, WorkerSettings
from app.schemas import TaskEnvelope
from runtime_support import AUTH
from test_agent_read import FIXTURE
from test_investigation import Policies, finding, finish, search
from test_shared_inbox import inbox_application
import test_workflow_integration as workflow


URL = os.getenv("AGENT_TEST_INBOX_DB_URL")
TRACE = FIXTURE["task"]["traceId"]


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_and_alias_groups_and_independent_sllm(self):
        self.assertEqual(ModelLimits(), ModelLimits(30, 60000, 4, 12000, 2000, 30))
        one = OpenAIInvestigationModel(model="alias-a", api_key="test", quota_group="shared")
        two = OpenAIInvestigationModel(model="alias-b", api_key="test", quota_group="shared")
        self.assertEqual(one.quota_key, two.quota_key)
        self.assertNotEqual(one.quota_key, OpenAIInvestigationModel(
            model="alias-a", api_key="test", endpoint="http://localhost:9999/v1").quota_key)
        with self.assertRaises(ValueError):
            OpenAIInvestigationModel(model="sllm", api_key="test", provider="openai-compatible")
        with patch.dict(os.environ, {
            "MODEL_PROVIDER": "openai-compatible", "MODEL_BASE_URL": "http://localhost:9999/v1",
            "MODEL_NAME": "sllm", "MODEL_API_KEY": "test", "MODEL_QUOTA_GROUP": "sllm-only",
            "MODEL_CONCURRENCY": "1",
        }):
            model = model_from_env()
            self.assertEqual(model.limits.concurrency, 1)
            self.assertEqual(model._model, "sllm")

    def test_retry_after_supports_seconds_dates_and_invalid_values(self):
        self.assertEqual(retry_after_seconds("17"), 17)
        value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20))
        self.assertGreater(retry_after_seconds(value), 18)
        for value in (None, "invalid", "-1", "NaN", "inf"):
            self.assertEqual(retry_after_seconds(value), 0)

    def test_optional_live_id_preserves_legacy_fingerprint(self):
        import hashlib
        import json
        task = TaskEnvelope.model_validate(FIXTURE["task"])
        payload, fingerprint = task_payload(task)
        self.assertNotIn("liveId", payload)
        old = {k: v for k, v in FIXTURE["task"].items() if k not in {"eventId", "traceId"}}
        # Defaults are part of the original normalized fingerprint.
        old = {**payload, **old}
        old.pop("eventId")
        old.pop("traceId")
        self.assertEqual(fingerprint, hashlib.sha256(
            json.dumps(old, sort_keys=True).encode()).hexdigest())

    async def test_graph_retry_budget_survives_gate_restart_and_is_not_inbox_attempts(self):
        async def no_sleep(_):
            pass
        gate = HarnessGate(trace_id=TRACE, sleep=no_sleep)
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls % 3:
                raise AgentToolError("MODEL_UNAVAILABLE", trace_id=TRACE, retryable=True)
            return "ok"

        await gate.run("model", flaky)
        await gate.run("model", flaky)
        restarted = HarnessGate.from_state({"trace_id": TRACE, "harness_budget": gate.snapshot()},
                                          sleep=no_sleep)
        with self.assertRaises(AgentToolError) as error:
            await restarted.run("model", flaky)
        self.assertEqual(error.exception.code, "EXECUTION_LIMIT")
        self.assertEqual(restarted.snapshot()["exhausted"], "MODEL_RETRIES")
        self.assertEqual(restarted.snapshot()["modelRetryCount"], 4)
        self.assertEqual(calls, 7)

    async def test_retry_after_is_not_shortened_by_backoff_cap(self):
        delays = []

        async def sleep(delay):
            delays.append(delay)
        gate = HarnessGate(trace_id=TRACE, sleep=sleep)

        async def unavailable():
            raise AgentToolError("MODEL_RATE_LIMITED", trace_id=TRACE,
                                 retryable=True, retry_after=45)
        with self.assertRaises(AgentToolError):
            await gate.run("model", unavailable)
        self.assertEqual(delays, [45, 45])

    async def test_input_limit_rejects_without_truncating_or_calling(self):
        calls = []

        async def complete(messages, tools, **kwargs):
            calls.append(messages)
        request = ModelRequest(SimpleNamespace(complete=complete),
                               [{"role": "user", "content": "가" * 12001}], [], trace_id=TRACE)
        with self.assertRaises(AgentToolError) as error:
            await HarnessGate(trace_id=TRACE).run("model", request)
        self.assertEqual(error.exception.code, "MODEL_CONTEXT_LIMIT")
        self.assertEqual(calls, [])
        self.assertEqual(len(request.messages[0]["content"]), 12001)

    async def test_model_timeout_is_retryable_and_sdk_independent(self):
        async def complete(*args, **kwargs):
            await asyncio.sleep(1)
        model = SimpleNamespace(complete=complete, limits=replace(ModelLimits(), timeout=0.01))
        request = ModelRequest(model, [], [], trace_id=TRACE)
        with self.assertRaises(AgentToolError) as error:
            async with request.admission():
                await request()
        self.assertEqual(error.exception.code, "MODEL_TIMEOUT")
        self.assertTrue(error.exception.retryable)

    async def test_known_output_above_cap_is_rejected_not_truncated(self):
        async def complete(*args, **kwargs):
            return ModelTurn([], {"inputTokens": 10, "outputTokens": 2001})
        request = ModelRequest(SimpleNamespace(complete=complete), [], [], trace_id=TRACE)
        with self.assertRaises(AgentToolError) as error:
            async with request.admission():
                await request()
        self.assertEqual(error.exception.code, "MODEL_TOKEN_LIMIT")
        self.assertEqual(error.exception.usage["outputTokens"], 2001)


@unittest.skipUnless(URL, "Postgres Inbox DB required")
class DistributedLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "limits_" + uuid4().hex
        self.store = PostgresInbox(URL, schema=self.schema)
        await asyncio.to_thread(self.store.setup)
        self.second = PostgresInbox(URL, schema=self.schema)
        self.task = TaskEnvelope.model_validate(FIXTURE["task"])
        self.stack = AsyncExitStack()
        self.workers = []
        self.release = asyncio.Event()
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        self.release.set()
        for worker in self.workers:
            worker.stop_claiming()
        for worker in self.workers:
            await worker.close()
        await self.stack.aclose()
        with self.store.connection() as db:
            for schema in (self.schema, self.schema + "_cp"):
                db.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    def task_for(self, live=None):
        return self.task.model_copy(update={"task_id": uuid4(), "thread_id": uuid4().hex,
                                           "live_id": live})

    def jobs(self, count):
        for _ in range(count):
            self.store.accept(self.task_for())
        return [self.store.claim(f"quota-worker-{i}", lease_seconds=5) for i in range(count)]

    def reserve(self, job, **kwargs):
        return self.store.reserve_model(job, **{
            "quota_key": "shared", "tokens": 14000, "rpm": 30, "tpm": 60000,
            "concurrency": 4, **kwargs})

    def expire(self, job):
        with self.store.connection() as db:
            db.execute(sql.SQL("""
                UPDATE {} SET lease_expires_at=clock_timestamp()-interval '1 second'
                WHERE operation_id=%s
            """).format(self.store._jobs), (job["operation_id"],))

    async def test_claim_race_enforces_cluster_four_and_worker_two_before_running(self):
        for _ in range(20):
            self.store.accept(self.task_for())
        with ThreadPoolExecutor(max_workers=12) as pool:
            claims = list(pool.map(lambda i: self.second.claim(f"worker-{i % 2}"), range(20)))
        jobs = [job for job in claims if job]
        self.assertEqual(len(jobs), 4)
        self.assertEqual(sum(j["worker_id"] == "worker-0" for j in jobs), 2)
        self.assertEqual(self.store.metrics()["queued"], 16)
        self.assertEqual(self.store.metrics()["running"], 4)
        for job in jobs:
            self.store.finish(job, "WAITING_REVIEW")
        self.assertIsNotNone(self.store.claim("worker-0"))
        self.assertEqual(self.store.metrics()["waiting_review"], 4)

    async def test_round_robin_hot_live_never_starves_cold_live_and_idle_can_fill_four(self):
        hot, cold = uuid4(), uuid4()
        for _ in range(10):
            self.store.accept(self.task_for(hot))
        self.store.accept(self.task_for(cold))
        jobs = [self.store.claim(f"worker-{i}") for i in range(4)]
        self.assertEqual([j["live_id"] for j in jobs[:2]], [str(hot), str(cold)])
        # Once cold is no longer pending, hot may use every remaining slot.
        self.assertEqual(sum(j["live_id"] == str(hot) for j in jobs), 3)
        self.store.finish(jobs[1], "WAITING_REVIEW")
        extra = self.store.claim("another-worker")
        self.assertEqual(extra["live_id"], str(hot))

    async def test_pending_other_live_caps_hot_to_two_slots(self):
        hot, cold = uuid4(), uuid4()
        for _ in range(6):
            self.store.accept(self.task_for(hot))
            self.store.accept(self.task_for(cold))
        jobs = [self.store.claim(f"worker-{i}") for i in range(4)]
        self.assertEqual([j["live_id"] for j in jobs], [str(hot), str(cold)] * 2)

    async def test_capacity_race_duplicate_and_status_allowed_with_503_retry_after(self):
        self.store.capacity = self.second.capacity = 2
        tasks = [self.task_for() for _ in range(10)]

        def accept(task):
            try:
                self.second.accept(task)
                return task
            except InboxCapacityExceeded:
                return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            accepted = [t for t in pool.map(accept, tasks) if t]
        self.assertEqual(len(accepted), 2)
        with TestClient(inbox_application(self.store)) as client:
            task = self.task_for()
            response = client.post(f"/agent/tasks/{task.task_id}/run", headers=AUTH,
                                   json=task.model_dump(mode="json", by_alias=True))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "10")
            self.assertEqual(response.json()["detail"]["code"], "INBOX_CAPACITY_EXCEEDED")
            old = accepted[0]
            response = client.post(f"/agent/tasks/{old.task_id}/run", headers=AUTH,
                                   json=old.model_dump(mode="json", by_alias=True))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.get(f"/agent/tasks/{old.task_id}", headers=AUTH).status_code, 200)
        self.assertEqual(self.store.metrics()["queued"], 2)

    async def test_capacity_includes_waiting_and_unknown_but_never_deletes_old_receipts(self):
        self.store.capacity = 1
        self.store.accept(self.task)
        job = self.store.claim("worker")
        self.store.finish(job, "WAITING_REVIEW")
        with self.assertRaises(InboxCapacityExceeded):
            self.store.accept(self.task_for())
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET status='UNKNOWN'").format(self.store._jobs))
        with self.assertRaises(InboxCapacityExceeded):
            self.store.accept(self.task_for())
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET status='SUCCEEDED'").format(self.store._jobs))
        self.store.accept(self.task_for())
        self.assertFalse(self.store.accept(self.task)[1])
        self.assertEqual(self.store.get(self.task.task_id)["status"], "SUCCEEDED")

    async def test_token_reservations_reconcile_known_and_retain_unknown_usage(self):
        one, two = self.jobs(2)
        reservation = self.reserve(one, tokens=40000)
        self.assertIsNone(self.reserve(two, tokens=21000))
        self.store.reconcile_model(reservation, {"inputTokens": 100, "outputTokens": 50})
        next_reservation = self.reserve(two, tokens=59000)
        self.assertIsNotNone(next_reservation)
        self.store.reconcile_model(next_reservation, {})
        self.assertIsNone(self.reserve(one, tokens=1000))
        with self.store.connection() as db:
            usage = db.execute(sql.SQL("SELECT sum(tokens) AS n FROM {}").format(
                self.store._reservations)).fetchone()["n"]
        self.assertEqual(usage, 59150)

    async def test_rpm_window_is_shared_across_instances_and_includes_failed_calls(self):
        job, = self.jobs(1)
        for _ in range(2):
            reservation = self.reserve(job, rpm=2)
            self.store.reconcile_model(reservation, {})
        self.assertIsNone(self.second.reserve_model(job, quota_key="shared", tokens=14000,
                                                    rpm=2, tpm=60000, concurrency=4))
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET created_at=clock_timestamp()-interval '61 seconds'")
                       .format(self.store._reservations))
        self.assertIsNotNone(self.reserve(job, rpm=2))

    async def test_global_model_four_even_across_distinct_groups_and_group_one(self):
        jobs = self.jobs(4)
        reservations = [self.reserve(job, quota_key=f"group-{i}", concurrency=1)
                        for i, job in enumerate(jobs)]
        self.assertTrue(all(reservations))
        self.assertIsNone(self.reserve(jobs[0], quota_key="fifth"))
        self.store.reconcile_model(reservations[0], {})
        self.assertIsNone(self.reserve(jobs[0], quota_key="group-1", concurrency=1))
        self.assertIsNotNone(self.reserve(jobs[0], quota_key="fifth"))
        self.assertEqual(self.store.metrics()["model_running"], 4)

    async def test_shared_cooldown_extends_not_shortens_and_other_endpoint_unaffected(self):
        job, = self.jobs(1)
        reservation = self.reserve(job)
        self.store.reconcile_model(reservation, {})
        self.store.model_cooldown("shared", 0.2)
        self.second.model_cooldown("shared", 0.01)
        self.assertIsNone(self.reserve(job))
        self.assertIsNotNone(self.reserve(job, quota_key="independent-endpoint"))
        await asyncio.sleep(0.25)
        self.assertIsNotNone(self.reserve(job))
        self.assertEqual(self.store.metrics()["counters"]["model_429"], 2)

    async def test_expiry_fences_old_owner_and_recovery_reuses_graph_capacity(self):
        jobs = self.jobs(4)
        old = jobs[0]
        self.assertIsNone(self.store.claim("extra"))
        self.expire(old)
        recovered = self.second.claim("replacement")
        self.assertEqual(recovered["operation_id"], old["operation_id"])
        with self.assertRaises(LeaseLost):
            self.reserve(old)
        with self.assertRaises(LeaseLost):
            self.store.heartbeat(old, lease_seconds=5)
        self.assertEqual(self.store.metrics()["running"], 4)
        self.assertEqual(self.store.metrics()["counters"]["lease_recoveries"], 1)

    async def test_mixed_cluster_configuration_fails_closed_at_startup(self):
        different = PostgresInbox(URL, schema=self.schema, graph_concurrency=8)
        with self.assertRaisesRegex(ValueError, "Inconsistent shared"):
            different.setup()

    async def test_legacy_live_bucket_is_explicit_and_never_derived_from_thread(self):
        jobs = self.jobs(2)
        self.assertEqual({j["live_id"] for j in jobs}, {"legacy:missing-live-id"})
        self.assertNotEqual(jobs[0]["thread_id"], jobs[1]["thread_id"])

    async def test_model_retry_cap_is_durable_even_without_a_completed_node_checkpoint(self):
        job, = self.jobs(1)
        guard = LeaseGuard(self.store, job)
        calls = 0

        async def unavailable():
            nonlocal calls
            calls += 1
            raise AgentToolError("MODEL_UNAVAILABLE", trace_id=TRACE, retryable=True)

        async def no_sleep(_):
            pass
        token = current_lease.set(guard)
        try:
            for _ in range(3):
                gate = HarnessGate(trace_id=TRACE, sleep=no_sleep)
                with self.assertRaises(AgentToolError):
                    await gate.run("model", unavailable)
        finally:
            current_lease.reset(token)
        self.assertEqual(calls, 7)
        self.assertEqual(gate.snapshot()["exhausted"], "MODEL_RETRIES")
        self.assertEqual(gate.snapshot()["modelRetryCount"], 4)
        self.assertEqual(self.store.metrics()["counters"]["model_retries"], 4)
        with self.store.connection() as db:
            row = db.execute(sql.SQL("SELECT attempts,model_retries,model_calls FROM {}").format(
                self.store._jobs)).fetchone()
        self.assertEqual((row["attempts"], row["model_retries"], row["model_calls"]), (1, 4, 7))

    async def test_model_call_cap_is_durable_across_gate_restarts(self):
        job, = self.jobs(1)
        guard = LeaseGuard(self.store, job)
        calls = 0

        async def unavailable():
            nonlocal calls
            calls += 1
            raise AgentToolError("MODEL_UNAVAILABLE", trace_id=TRACE, retryable=False)

        token = current_lease.set(guard)
        try:
            for _ in range(8):
                with self.assertRaises(AgentToolError) as error:
                    await HarnessGate(trace_id=TRACE).run("model", unavailable)
                self.assertEqual(error.exception.code, "MODEL_UNAVAILABLE")
            with self.assertRaises(AgentToolError) as error:
                await HarnessGate(trace_id=TRACE).run("model", unavailable)
        finally:
            current_lease.reset(token)
        self.assertEqual(error.exception.code, "EXECUTION_LIMIT")
        self.assertEqual(calls, 8)
        with self.store.connection() as db:
            row = db.execute(sql.SQL("SELECT model_calls FROM {}").format(
                self.store._jobs)).fetchone()
        self.assertEqual(row["model_calls"], 8)

    async def test_wait_age_metrics_authenticated_and_contain_no_identifiers_or_secrets(self):
        self.store.accept(self.task)
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET created_at=clock_timestamp()-interval '31 seconds'")
                       .format(self.store._jobs))
        self.assertEqual(self.store.metrics()["wait_severity"], "warning")
        with self.store.connection() as db:
            db.execute(sql.SQL("UPDATE {} SET created_at=clock_timestamp()-interval '121 seconds'")
                       .format(self.store._jobs))
        with TestClient(inbox_application(self.store)) as client:
            self.assertEqual(client.get("/agent/metrics").status_code, 401)
            response = client.get("/agent/metrics", headers=AUTH)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["wait_severity"], "critical")
            self.assertNotIn(str(self.task.task_id), response.text)
            self.assertNotIn(TRACE, response.text)

    async def test_model_reservation_cleanup_removes_stale_inactive_rows_only(self):
        job, = self.jobs(1)
        old = self.reserve(job)
        fresh = self.reserve(job, quota_key="fresh", concurrency=2)
        active = self.reserve(job, quota_key="active", concurrency=2)
        self.store.reconcile_model(old, {"inputTokens": 1, "outputTokens": 1})
        self.store.reconcile_model(fresh, {"inputTokens": 1, "outputTokens": 1})
        with self.store.connection() as db:
            db.execute(sql.SQL("""
                UPDATE {} SET created_at=clock_timestamp()-interval '71 seconds'
                WHERE reservation_id=%s
            """).format(self.store._reservations), (old,))
            db.execute(sql.SQL("""
                UPDATE {} SET created_at=clock_timestamp()-interval '30 seconds'
                WHERE reservation_id=%s
            """).format(self.store._reservations), (fresh,))
        self.store.reserve_model(job, quota_key="cleanup", tokens=14000,
                                 rpm=30, tpm=60000, concurrency=4)
        with self.store.connection() as db:
            rows = {str(row["reservation_id"]): row["active"] for row in db.execute(
                sql.SQL("SELECT reservation_id,active FROM {}").format(self.store._reservations))}
        self.assertNotIn(str(old), rows)
        self.assertIn(str(fresh), rows)
        self.assertIn(str(active), rows)

    async def test_quota_wait_keeps_heartbeat_and_cancellation_fencing(self):
        one, two = self.jobs(2)
        first = self.reserve(one, concurrency=1)
        called = []

        async def complete(*args, **kwargs):
            called.append(True)
            return ModelTurn([], {})
        guard = LeaseGuard(self.store, two)
        token = current_lease.set(guard)
        try:
            request = ModelRequest(SimpleNamespace(complete=complete, quota_key="shared",
                                  limits=replace(ModelLimits(), concurrency=1)), [], [], trace_id=TRACE)
            waiting = asyncio.create_task(HarnessGate(trace_id=TRACE).run("model", request))
        finally:
            current_lease.reset(token)
        for _ in range(3):
            await asyncio.sleep(0.1)
            self.store.heartbeat(two, lease_seconds=0.4)
        self.assertFalse(waiting.done())
        self.assertEqual(self.store.metrics()["model_waiting"], 1)
        with self.store.connection() as db:
            db.execute(sql.SQL("""
                UPDATE {} SET model_wait_since=clock_timestamp()-interval '31 seconds'
                WHERE operation_id=%s
            """).format(self.store._jobs), (two["operation_id"],))
        self.assertEqual(self.store.metrics()["wait_severity"], "warning")
        self.expire(two)
        with self.assertRaises(LeaseLost):
            await asyncio.wait_for(waiting, 1)
        self.assertTrue(guard.lost)
        self.assertEqual(called, [])
        self.store.reconcile_model(first, {})

    async def test_model_429_sets_shared_cooldown_and_timeout_metrics(self):
        job, = self.jobs(1)
        guard = LeaseGuard(self.store, job)

        async def unavailable(*args, **kwargs):
            raise AgentToolError("MODEL_RATE_LIMITED", trace_id=TRACE,
                                 retryable=True, retry_after=20)
        token = current_lease.set(guard)
        try:
            request = ModelRequest(SimpleNamespace(complete=unavailable, quota_key="shared"),
                                   [], [], trace_id=TRACE)
            with self.assertRaises(AgentToolError):
                async with request.admission():
                    await request()
        finally:
            current_lease.reset(token)
        self.assertIsNone(self.reserve(job))
        self.assertEqual(self.store.metrics()["model_running"], 0)
        self.assertEqual(self.store.metrics()["counters"]["model_429"], 1)

    async def test_model_timeout_and_known_error_usage_reconciliation_are_recorded(self):
        job, = self.jobs(1)
        guard = LeaseGuard(self.store, job)

        async def invalid(*args, **kwargs):
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=TRACE,
                                 usage={"inputTokens": 10, "outputTokens": 2})

        async def slow(*args, **kwargs):
            await asyncio.sleep(1)
        token = current_lease.set(guard)
        try:
            request = ModelRequest(SimpleNamespace(complete=invalid), [], [], trace_id=TRACE)
            with self.assertRaises(AgentToolError):
                async with request.admission():
                    await request()
            timeout = ModelRequest(SimpleNamespace(complete=slow,
                                   limits=replace(ModelLimits(), timeout=0.01)), [], [], trace_id=TRACE)
            with self.assertRaises(AgentToolError):
                async with timeout.admission():
                    await timeout()
        finally:
            current_lease.reset(token)
        with self.store.connection() as db:
            rows = db.execute(sql.SQL("SELECT tokens FROM {} ORDER BY created_at").format(
                self.store._reservations)).fetchall()
        self.assertEqual(rows[0]["tokens"], 12)
        self.assertEqual(rows[1]["tokens"], timeout.input_tokens + 2000)
        self.assertEqual(self.store.metrics()["model_running"], 0)
        self.assertEqual(self.store.metrics()["counters"]["model_timeouts"], 1)

    async def test_shutdown_fences_every_cancellation_resistant_lane(self):
        saver = await self.stack.enter_async_context(open_checkpointer(
            URL, schema=self.schema + "_cp", saver_type=FencedPostgresSaver))
        worker = GraphWorker(self.store, SimpleNamespace(checkpointer=saver),
            gateway=SimpleNamespace(), settings=WorkerSettings(worker_id="shutdown",
                lease_seconds=2, heartbeat_seconds=0.1, poll_seconds=0.01, shutdown_seconds=0.05))
        self.workers.append(worker)
        guards = []
        entered = asyncio.Event()
        rejected = []

        async def execute(guard):
            guards.append(guard)
            if len(guards) == 2:
                entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
            try:
                await guard.check()
            except LeaseLost:
                rejected.append(guard)

        worker._execute = execute
        self.store.accept(self.task_for())
        self.store.accept(self.task_for())
        worker.start()
        await asyncio.wait_for(entered.wait(), 2)
        await worker.close()
        self.assertEqual(len(guards), 2)
        self.assertTrue(all(guard.lost for guard in guards))
        self.release.set()
        await asyncio.gather(worker.worker, return_exceptions=True)
        self.assertEqual(len(rejected), 2)

    async def test_two_real_workers_graph_and_model_bound_four_heartbeat_and_drain(self):
        await workflow.WorkflowTests.prepare(self)
        active = peak = 0
        entered = asyncio.Event()
        state = self.state
        release = self.release

        class Model:
            async def complete(model_self, messages, tools, **kwargs):
                nonlocal active, peak
                active += 1
                peak = max(active, peak)
                if active == 4:
                    entered.set()
                try:
                    await release.wait()
                    call = (finish([finding(state)]) if any(m["role"] == "tool" for m in messages)
                            else search())
                    return ModelTurn([call], {"inputTokens": 10, "outputTokens": 2})
                finally:
                    active -= 1

        for i in range(2):
            saver = await self.stack.enter_async_context(open_checkpointer(
                URL, schema=self.schema + "_cp", saver_type=FencedPostgresSaver))
            graph = build_moderation_graph(model=Model(), business=self.business, policy=Policies(),
                backend=self.backend, checkpointer=saver, observer=lambda *_: None)
            worker = GraphWorker(self.second, graph, gateway=self.backend, settings=WorkerSettings(
                worker_id=f"actual-{i}", lease_seconds=2, heartbeat_seconds=0.1,
                poll_seconds=0.01, shutdown_seconds=2))
            self.workers.append(worker)
        tasks = [self.task_for() for _ in range(8)]
        for task in tasks:
            self.store.accept(task)
        for worker in self.workers:
            worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0.3)
        metrics = self.store.metrics()
        self.assertEqual((metrics["running"], metrics["queued"], metrics["model_running"]), (4, 4, 4))
        self.assertEqual(metrics["expired_leases"], 0)
        self.release.set()
        async with asyncio.timeout(10):
            while any(self.store.get(task.task_id)["status"] != "WAITING_REVIEW" for task in tasks):
                await asyncio.sleep(0.02)
        self.assertEqual(peak, 4)
        self.assertEqual(self.store.metrics()["queued"], 0)
        self.assertEqual(self.store.metrics()["running"], 0)
        self.assertEqual(self.store.metrics()["waiting_review"], 8)
