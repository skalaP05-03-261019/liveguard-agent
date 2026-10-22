"""Authenticated durable HTTP task inbox. 202 means persisted, not executed."""
import hmac
import os
import sqlite3
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
import psycopg
from starlette.concurrency import run_in_threadpool

from app.runtime.bootstrap import configured_runtime
from app.runtime.inbox import configured_inbox
from app.integration.preflight import configured_read_tools, inspect_task
from app.runtime.store import TaskConflict
from app.schemas import TaskEnvelope


def create_app(*, runtime_factory=configured_runtime, service_token=None,
               preflight_tools_factory=configured_read_tools, inbox_factory=configured_inbox):
    @asynccontextmanager
    async def lifespan(application):
        application.state.service_token = (service_token if service_token is not None
                                           else os.getenv("AGENT_SERVICE_TOKEN", ""))
        async with AsyncExitStack() as stack:
            inbox = await stack.enter_async_context(inbox_factory())
            # Shared intake never starts a Graph or requires model/MCP credentials.
            runtime = None if inbox is not None else await stack.enter_async_context(runtime_factory())
            application.state.inbox = inbox
            application.state.runtime = runtime
            if runtime and application.state.service_token.strip():
                runtime.start()
            try:
                yield
            finally:
                if runtime:
                    await runtime.close()
                application.state.runtime = None
                application.state.inbox = None

    application = FastAPI(title="LIVE:GUARD Agent", version="0.2.0", lifespan=lifespan)

    async def authenticate(authorization: str | None = Header(default=None)):
        expected = getattr(application.state, "service_token", "")
        if not expected or not expected.strip():
            raise HTTPException(503, {"code": "SERVICE_AUTH_NOT_CONFIGURED"})
        provided = authorization.removeprefix("Bearer ") if authorization else ""
        if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(
                provided.encode(), expected.encode()):
            raise HTTPException(401, {"code": "UNAUTHENTICATED"}, headers={"WWW-Authenticate": "Bearer"})

    def ready_runtime(*, require_worker=True):
        runtime = getattr(application.state, "runtime", None)
        if runtime is None or (require_worker and not runtime.ready):
            raise HTTPException(503, {"code": "AGENT_RUNTIME_NOT_READY"})
        return runtime

    @application.get("/health")
    async def health():
        inbox = getattr(application.state, "inbox", None)
        if inbox is not None:
            try:
                await run_in_threadpool(inbox.check)
                ready = bool(getattr(application.state, "service_token", "").strip())
            except psycopg.Error:
                ready = False
            return {"status": "ready" if ready else "not_ready", "readyForTasks": ready}
        runtime = getattr(application.state, "runtime", None)
        ready = bool(runtime and runtime.ready and getattr(application.state, "service_token", "").strip())
        return {"status": "ready" if ready else "not_ready", "readyForTasks": ready}

    async def accept(task_id, task, *, resume):
        if task_id != task.task_id:
            raise HTTPException(400, "URL taskId and body taskId must match")
        if resume:
            if task.kind != "RESUME_REVIEW" or task.review_kind is None or task.review_id is None:
                raise HTTPException(400, "RESUME_REVIEW requires reviewKind and reviewId")
        elif task.kind not in {"ANALYZE", "REANALYZE"}:
            raise HTTPException(400, "Use /resume for reviews; RECONCILE is not implemented")
        inbox = getattr(application.state, "inbox", None)
        store = inbox if inbox is not None else ready_runtime().store
        try:
            record, created = await run_in_threadpool(store.accept, task)
        except TaskConflict as error:
            raise HTTPException(409, {"code": str(error)}) from None
        except (sqlite3.Error, psycopg.Error):
            raise HTTPException(503, {"code": "TASK_STORE_UNAVAILABLE"}) from None
        return JSONResponse(status_code=202 if created else 200,
                            content={**record, "duplicate": not created})

    @application.post("/agent/tasks/{task_id}/run", dependencies=[Depends(authenticate)])
    async def run_task(task_id: UUID, task: TaskEnvelope):
        return await accept(task_id, task, resume=False)

    @application.post("/agent/tasks/{task_id}/preflight", dependencies=[Depends(authenticate)])
    async def preflight(task_id: UUID, task: TaskEnvelope):
        # Available without model/Gateway; never accepts a job or resumes a decision.
        if task_id != task.task_id or task.kind not in {"ANALYZE", "REANALYZE"}:
            raise HTTPException(400, {"code": "INVALID_PREFLIGHT_TASK"})
        try:
            tools = preflight_tools_factory()
        except (ValueError, TypeError):
            raise HTTPException(503, {"code": "BUSINESS_MCP_NOT_CONFIGURED"}) from None
        try:
            return await inspect_task(task, tools)
        except Exception:
            raise HTTPException(503, {"code": "PREFLIGHT_UNAVAILABLE"}) from None

    @application.post("/agent/tasks/{task_id}/resume", dependencies=[Depends(authenticate)])
    async def resume_task(task_id: UUID, task: TaskEnvelope):
        return await accept(task_id, task, resume=True)

    @application.get("/agent/tasks/{task_id}", dependencies=[Depends(authenticate)])
    async def get_task(task_id: UUID):
        inbox = getattr(application.state, "inbox", None)
        runtime = getattr(application.state, "runtime", None)
        if inbox is None and runtime is None:
            raise HTTPException(503, {"code": "AGENT_RUNTIME_NOT_READY"})
        store = inbox if inbox is not None else runtime.store
        try:
            record = await run_in_threadpool(store.get, task_id)
        except (sqlite3.Error, psycopg.Error):
            raise HTTPException(503, {"code": "TASK_STORE_UNAVAILABLE"}) from None
        if record is None:
            raise HTTPException(404, {"code": "TASK_NOT_FOUND"})
        return record

    return application


app = create_app()
