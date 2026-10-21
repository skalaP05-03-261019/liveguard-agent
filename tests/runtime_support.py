"""HTTP test composition uses temporary durable inbox and controlled Graph."""
from contextlib import asynccontextmanager
import time

from app.main import create_app
from app.runtime.runner import TaskRunner
from app.runtime.store import TaskStore
from app.schemas import TaskEnvelope

AUTH = {"Authorization": "Bearer test-inbound-token"}


def test_application(path, graph, origin=None):
    store = TaskStore(path)
    if origin:
        task = TaskEnvelope.model_validate({**origin, "kind": "ANALYZE", "decisionId": None,
                                            "reviewKind": None, "reviewId": None})
        record, _ = store.accept(task)
        store.finish(record["operationId"], "WAITING_REVIEW")
    @asynccontextmanager
    async def factory():
        yield TaskRunner(store, graph)
    return create_app(runtime_factory=factory, service_token="test-inbound-token"), store


def wait_for_task(client, task_id):
    for _ in range(150):
        result = client.get(f"/agent/tasks/{task_id}", headers=AUTH)
        if result.json().get("status") not in {"QUEUED", "RUNNING"}:
            return result.json()
        time.sleep(0.01)
    raise AssertionError("Worker did not finish")
