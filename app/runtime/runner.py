"""Drive the existing approval Graph; never infer or replay business mutations."""
import asyncio

from app.harness.resume import ResumeError, resume_review, thread_config
from app.schemas import TaskEnvelope


class TaskRunner:
    def __init__(self, store, graph):
        self.store, self.graph = store, graph
        self.worker = None
        self.failure = False

    def start(self):
        self.worker = asyncio.create_task(self.serve())

    @property
    def ready(self):
        return self.worker is not None and not self.worker.done() and not self.failure

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass

    async def serve(self):
        try:
            while True:
                if not await self.run_one():
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            # No exception text: dependency errors may include credentials or chat text.
            self.failure = True

    async def run_one(self):
        job = self.store.claim()
        if job is None:
            return False
        task = TaskEnvelope.model_validate_json(job["envelope"])
        key = job["operation_id"]
        try:
            if task.kind == "RESUME_REVIEW":
                await resume_review(self.graph, review_kind=task.review_kind, thread_id=task.thread_id,
                                    review_id=task.review_id, decision_id=task.decision_id, case_id=task.case_id)
            else:
                # A checkpoint with this thread may predate this inbox. Never overwrite it.
                previous = await self.graph.aget_state(thread_config(task.thread_id))
                if previous.created_at is not None:
                    self.store.finish(key, "FAILED", "THREAD_CHECKPOINT_EXISTS")
                    return True
                await self.graph.ainvoke({"task": task}, thread_config(task.thread_id))
            state = await self.graph.aget_state(thread_config(task.thread_id))
            status = "WAITING_REVIEW" if state.interrupts else state.values.get("task_outcome", "UNKNOWN")
            if status not in {"WAITING_REVIEW", "SUCCEEDED", "FAILED", "UNKNOWN", "NEEDS_REVIEW"}:
                status = "UNKNOWN"
            self.store.finish(key, status)
        except ResumeError as error:
            # No Command was applied. Keep the thread available for a corrected decision.
            state = await self.graph.aget_state(thread_config(task.thread_id))
            self.store.finish(key, "WAITING_REVIEW" if state.interrupts else "FAILED", error.code)
        except asyncio.CancelledError:
            self.store.finish(key, "UNKNOWN", "EXECUTION_INTERRUPTED")
            raise
        except Exception:
            self.store.finish(key, "UNKNOWN", "EXECUTION_UNCONFIRMED")
        return True
