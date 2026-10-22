"""Standalone shared-Inbox executor. No external work inside inbox transactions."""
import asyncio
from dataclasses import dataclass
import math
import os
from uuid import uuid4

from app.clients.business_mcp import AgentToolError
from app.harness.resume import ResumeError, resume_review, thread_config
from app.harness.context_review import CONTEXT_DECISIONS
from app.runtime.fencing import FencedPostgresSaver, GuardedGateway, LeaseGuard, current_lease
from app.runtime.inbox import LeaseLost
from app.runtime.outcomes import TERMINAL, checkpoint_outcome
from app.runtime.recovery import _bound, _decision_bound, _waiting_is_bound
from app.schemas import TaskEnvelope


@dataclass(frozen=True)
class WorkerSettings:
    worker_id: str = ""
    lease_seconds: float = 30
    heartbeat_seconds: float = 5
    poll_seconds: float = 0.5
    shutdown_seconds: float = 30
    max_attempts: int = 3
    backoff_seconds: float = 1

    def __post_init__(self):
        if (not self.worker_id or len(self.worker_id) > 128 or self.max_attempts < 1
                or any(not math.isfinite(v) or v <= 0 for v in (
                    self.lease_seconds, self.heartbeat_seconds, self.poll_seconds,
                    self.shutdown_seconds, self.backoff_seconds))
                or self.heartbeat_seconds * 3 > self.lease_seconds):
            raise ValueError("Invalid worker settings")

    @classmethod
    def from_env(cls):
        return cls(
            worker_id=os.getenv("AGENT_WORKER_ID") or str(uuid4()),
            lease_seconds=float(os.getenv("AGENT_WORKER_LEASE_SECONDS", "30")),
            heartbeat_seconds=float(os.getenv("AGENT_WORKER_HEARTBEAT_SECONDS", "5")),
            poll_seconds=float(os.getenv("AGENT_WORKER_POLL_SECONDS", "0.5")),
            shutdown_seconds=float(os.getenv("AGENT_WORKER_SHUTDOWN_SECONDS", "30")),
            max_attempts=int(os.getenv("AGENT_WORKER_MAX_ATTEMPTS", "3")),
            backoff_seconds=float(os.getenv("AGENT_WORKER_BACKOFF_SECONDS", "1")))


class WorkerFailure(Exception):
    """Safe readiness/exit code; dependency exception text must not be logged."""


class GraphWorker:
    def __init__(self, inbox, graph, *, gateway, settings=None):
        if not isinstance(graph.checkpointer, FencedPostgresSaver):
            raise ValueError("Shared workers require a fenced PostgreSQL checkpointer")
        self.store, self.graph = inbox, graph
        self.gateway = GuardedGateway(gateway)
        self.settings = settings or WorkerSettings.from_env()
        self.stopping = asyncio.Event()
        self.worker = None
        self.failure_code = None
        self.active_guard = None
        self.active_heartbeat = None

    @property
    def ready(self):
        return bool(self.worker and not self.worker.done()
                    and not self.stopping.is_set() and not self.failure_code)

    def start(self):
        if self.worker is not None:
            raise RuntimeError("Worker already started")
        self.worker = asyncio.create_task(self.serve())

    def stop_claiming(self):
        self.stopping.set()

    async def close(self):
        self.stop_claiming()
        if self.worker is None or self.worker.cancelled():
            return
        try:
            await asyncio.wait_for(asyncio.shield(self.worker), self.settings.shutdown_seconds)
        except TimeoutError:
            # Cancellation can be delayed by an in-flight dependency. Fence it
            # before cancellation so it cannot authorize any subsequent effect.
            if self.active_guard is not None:
                self.active_guard.lost = True
            if self.active_heartbeat is not None:
                self.active_heartbeat.cancel()
            self.worker.cancel()
            done, _ = await asyncio.wait({self.worker}, timeout=self.settings.shutdown_seconds)
            if done and not self.worker.cancelled():
                self.worker.result()

    async def serve(self):
        try:
            while not self.stopping.is_set():
                if not await self.run_one():
                    try:
                        await asyncio.wait_for(self.stopping.wait(), self.settings.poll_seconds)
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure_code = "WORKER_RUNTIME_UNAVAILABLE"
            raise WorkerFailure(self.failure_code) from None

    async def _heartbeat(self, guard, execution):
        try:
            while True:
                await asyncio.sleep(self.settings.heartbeat_seconds)
                await asyncio.to_thread(self.store.heartbeat, guard.job,
                                        lease_seconds=self.settings.lease_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            guard.lost = True
            execution.cancel()

    async def run_one(self):
        if self.stopping.is_set():
            return False
        job = await asyncio.to_thread(self.store.claim, self.settings.worker_id,
            lease_seconds=self.settings.lease_seconds, max_attempts=self.settings.max_attempts)
        if job is None:
            return False
        guard = LeaseGuard(self.store, job)
        execution = asyncio.create_task(self._execute(guard))
        heartbeat = asyncio.create_task(self._heartbeat(guard, execution))
        self.active_guard = guard
        self.active_heartbeat = heartbeat
        try:
            await execution
        except LeaseLost:
            guard.lost = True
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not guard.lost:
                raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.active_guard = None
            self.active_heartbeat = None
        return True

    async def _finish(self, guard, status, code=None):
        await guard.check()
        await asyncio.to_thread(self.store.finish, guard.job, status, code)

    async def _retry(self, guard):
        await guard.check()
        await asyncio.to_thread(self.store.retry_unstarted, guard.job,
            max_attempts=self.settings.max_attempts, backoff_seconds=self.settings.backoff_seconds)

    async def _retry_recovery_read(self, guard, read):
        try:
            await read
        except AgentToolError as error:
            if error.code != "BACKEND_DELIVERY_UNKNOWN":
                raise
            await guard.check()
            if guard.job["attempts"] >= self.settings.max_attempts:
                await self._finish(guard, "UNKNOWN", "DECISION_REVALIDATION_UNAVAILABLE")
            else:
                await asyncio.to_thread(self.store.retry_recovery, guard.job,
                    backoff_seconds=self.settings.backoff_seconds)
            return False
        return True

    async def _execute(self, guard):
        token = current_lease.set(guard)
        job = guard.job
        try:
            task = TaskEnvelope.model_validate_json(job["envelope"])
            if self.stopping.is_set() and not job["started"]:
                await self._retry(guard)
                return
            if job["recovery_required"]:
                await self._recover(guard, task, allow_resume=True)
                return
            config = thread_config(task.thread_id)
            if task.kind != "RESUME_REVIEW":
                previous = await self.graph.aget_state(config)
                await guard.check()
                if previous.created_at is not None:
                    await self._finish(guard, "FAILED", "THREAD_CHECKPOINT_EXISTS")
                    return
            else:
                await self._prepare_resume(guard, task)
            await asyncio.to_thread(self.store.mark_started, job)
            job["started"] = True
            if task.kind == "RESUME_REVIEW":
                await resume_review(self.graph, review_kind=task.review_kind, thread_id=task.thread_id,
                    review_id=task.review_id, decision_id=task.decision_id, case_id=task.case_id)
            else:
                await self.graph.ainvoke({"task": task}, config)
            snapshot = await self.graph.aget_state(config)
            status, code = checkpoint_outcome(snapshot)
            if status == "UNKNOWN" and job["mutation_started"]:
                await self._observe_backend(guard, task)
            else:
                await self._finish(guard, status, code)
        except LeaseLost:
            guard.lost = True
        except ResumeError as error:
            # Reject this operation, not the valid approval checkpoint. A later
            # corrected decision may still resume it; no Command has been sent.
            await self._finish(guard, "FAILED", error.code)
        except asyncio.CancelledError:
            # Child tasks inherit this guard too. Block late writes immediately,
            # keep the durable claim for expiry/reconciliation by another worker.
            guard.lost = True
            raise
        except Exception:
            await guard.check()
            if not job["started"]:
                await self._retry(guard)
            else:
                await self._recover(guard, task, allow_resume=False)
        finally:
            current_lease.reset(token)

    async def _prepare_resume(self, guard, task, snapshot=None):
        if snapshot is None:
            snapshot = await self.graph.aget_state(thread_config(task.thread_id))
        await guard.check()

        def reject(code):
            raise ResumeError(code, code, status_code=409)

        if snapshot.created_at is None:
            reject("THREAD_NOT_FOUND")
        if not _bound(task, snapshot.values, require_decision=False):
            reject("CHECKPOINT_BINDING_MISMATCH")
        if not snapshot.interrupts:
            reject("NO_PENDING_APPROVAL")
        if not _waiting_is_bound(task, snapshot):
            reject("CHECKPOINT_BINDING_MISMATCH")
        card = snapshot.interrupts[0].value
        if card["reviewKind"] != task.review_kind:
            reject("REVIEW_KIND_MISMATCH")
        if card["reviewId"] != str(task.review_id):
            reject("REVIEW_MISMATCH")
        await self._verify_decision(guard, task, card)

    async def _verify_decision(self, guard, task, refs):
        def reject(code):
            raise ResumeError(code, code, status_code=409)

        # Existing BE GET binds the decision to its review and returns kind,
        # case/live/snapshot/evidence/policy/thread refs. Never trust the envelope
        # as authorization, and never consume an interrupt on invalid data.
        try:
            stored = await self.gateway.get_decision(review_id=str(task.review_id),
                decision_id=str(task.decision_id), trace_id=str(task.trace_id))
        except AgentToolError as error:
            if error.code == "NOT_FOUND":
                reject("DECISION_NOT_FOUND")
            if error.code in {"INVALID_BACKEND_RESULT", "VALIDATION_FAILED", "FORBIDDEN"}:
                reject("DECISION_NOT_VERIFIED")
            raise
        refs = {key: refs[key] for key in (
            "reviewId", "caseId", "liveId", "snapshotId", "evidenceVersion",
            "policyVersion", "threadId")}
        refs.update(decisionId=str(task.decision_id), kind=task.review_kind, status="DECIDED")
        allowed = CONTEXT_DECISIONS if task.review_kind == "CONTEXT" else ("APPROVE", "REJECT")
        if (not isinstance(stored, dict) or any(stored.get(k) != v for k, v in refs.items())
                or type(stored.get("evidenceVersion")) is not int
                or stored.get("decision") not in allowed
                or ("value" in stored and stored["value"] != stored.get("decision"))):
            reject("DECISION_BINDING_MISMATCH")
        await guard.check()

    async def _recover(self, guard, task, *, allow_resume):
        await guard.check()
        snapshot = await self.graph.aget_state(thread_config(task.thread_id))
        await guard.check()
        bound = snapshot.created_at is not None and _bound(
            task, snapshot.values, require_decision=False)
        if bound and not any(item.error for item in snapshot.tasks):
            if snapshot.interrupts and _waiting_is_bound(task, snapshot):
                if task.kind != "RESUME_REVIEW" or _bound(task, snapshot.values):
                    await self._finish(guard, "WAITING_REVIEW", "CHECKPOINT_WAITING_CONFIRMED")
                    return
                if (allow_resume and not guard.job["mutation_started"]
                        and guard.job["attempts"] <= self.settings.max_attempts):
                    # Crash before consuming the approval: validate again and
                    # deliver only this same durable operation, never a new one.
                    if not await self._retry_recovery_read(
                            guard, self._prepare_resume(guard, task, snapshot)):
                        return
                    await resume_review(self.graph, review_kind=task.review_kind,
                        thread_id=task.thread_id, review_id=task.review_id,
                        decision_id=task.decision_id, case_id=task.case_id)
                    await self._recover(guard, task, allow_resume=False)
                    return
            if not snapshot.next and not snapshot.interrupts:
                status, code = checkpoint_outcome(snapshot)
                if status in TERMINAL and _bound(task, snapshot.values):
                    await self._finish(guard, status, code)
                    return
            if (allow_resume and snapshot.next and not snapshot.interrupts
                    and not guard.job["mutation_started"]
                    and guard.job["attempts"] <= self.settings.max_attempts):
                if task.kind == "RESUME_REVIEW":
                    if not _decision_bound(task, snapshot.values, require_result=False):
                        await self._finish(guard, "UNKNOWN", "CHECKPOINT_BINDING_UNVERIFIED")
                        return
                    values = snapshot.values
                    if not await self._retry_recovery_read(guard, self._verify_decision(guard, task, {
                        "reviewId": str(task.review_id), "caseId": values["case_id"],
                        "liveId": values["live_id"], "snapshotId": values["snapshot_id"],
                        "evidenceVersion": values["evidence_version"],
                        "policyVersion": values["policy_version"], "threadId": values["thread_id"]})):
                        return
                # Continue durable pending nodes, never re-invoke the initial
                # task. Mutation intent rules out replay.
                await self.graph.ainvoke(None, thread_config(task.thread_id))
                await self._recover(guard, task, allow_resume=False)
                return
        if guard.job["mutation_started"]:
            await self._observe_backend(guard, task)
        else:
            await self._finish(guard, "UNKNOWN", "CHECKPOINT_EXECUTION_UNCONFIRMED")

    async def _observe_backend(self, guard, task):
        await guard.check()
        action = guard.job["action_id"]
        try:
            if action:
                observed = await self.gateway.read_action(action_id=str(action), trace_id=str(task.trace_id))
                if (not isinstance(observed, dict) or observed.get("actionId") != str(action)
                        or observed.get("status") not in {"APPLIED", "SUCCEEDED", "FAILED", "UNKNOWN"}):
                    await self._finish(guard, "UNKNOWN", "INVALID_BACKEND_OBSERVATION")
                    return
                if observed["status"] != "UNKNOWN":
                    # Confirming an action cannot prove all pending Graph steps ran.
                    await self._finish(guard, "NEEDS_REVIEW", "ACTION_CONFIRMED_REQUIRES_REVIEW")
                    return
            else:
                observed = await self.gateway.read_case(case_id=str(task.case_id), trace_id=str(task.trace_id))
                refs = {"caseId": str(task.case_id), "snapshotId": str(task.snapshot_id),
                        "evidenceVersion": task.expected_evidence_version}
                if not isinstance(observed, dict) or any(observed.get(k) != v for k, v in refs.items()):
                    await self._finish(guard, "UNKNOWN", "INVALID_BACKEND_OBSERVATION")
                    return
                await self._finish(guard, "UNKNOWN", "BACKEND_STATE_REQUIRES_REVIEW")
                return
        except LeaseLost:
            raise
        except Exception:
            await self._finish(guard, "UNKNOWN", "BACKEND_RECONCILIATION_UNAVAILABLE")
            return
        await self._finish(guard, "UNKNOWN", "BACKEND_RECONCILIATION_REQUIRED")
