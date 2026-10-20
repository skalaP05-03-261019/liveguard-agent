"""API 경계만 제공한다. 작업 저장·실행 연결 전에는 202로 수락하지 않는다."""

from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.harness.resume import ResumeError, resume_review
from app.schemas import TaskEnvelope

app = FastAPI(title="LIVE:GUARD Agent", version="0.1.0")


@app.get("/health")
async def health():
    return {"status": "scaffold", "readyForTasks": False}


@app.post("/agent/tasks/{task_id}/run", responses={501: {"description": "Scaffold only"}})
async def run_task(task_id: UUID, task: TaskEnvelope):
    if task_id != task.task_id:
        raise HTTPException(400, "URL taskId and body taskId must match")
    if task.kind == "RESUME_REVIEW":
        raise HTTPException(400, "Use /resume for RESUME_REVIEW")
    # TODO: 서비스 인증·작업 영속화·중복 방지·실행기 연결 후에만 202 반환.
    raise HTTPException(501, "Task persistence and Agent runner are not connected")


@app.post(
    "/agent/tasks/{task_id}/resume",
    status_code=202,
    responses={
        404: {"description": "No checkpoint for threadId"},
        409: {"description": "No pending review (already resumed), or review kind/case/review mismatch"},
        501: {"description": "Checkpointed Agent graph is not configured"},
    },
)
async def resume_task(task_id: UUID, task: TaskEnvelope):
    if task_id != task.task_id:
        raise HTTPException(400, "URL taskId and body taskId must match")
    if task.kind != "RESUME_REVIEW":
        raise HTTPException(400, "Expected RESUME_REVIEW")
    # checkpointer가 붙은 Graph는 실행기 연결(#10)에서 app.state.agent_graph로 주입한다.
    graph = getattr(app.state, "agent_graph", None)
    if graph is None:
        raise HTTPException(501, "Checkpointed Agent graph is not configured")
    if task.review_kind is None or task.review_id is None:
        raise HTTPException(400, "RESUME_REVIEW requires reviewKind (CONTEXT or KICK) and reviewId")
    try:
        # 결정 내용은 decisionId로만 받고, 결정·승인 여부는 Graph가 Backend 저장본으로 확인한다.
        result = await resume_review(
            graph, review_kind=task.review_kind, thread_id=task.thread_id,
            review_id=task.review_id, decision_id=task.decision_id, case_id=task.case_id,
        )
    except ResumeError as error:
        raise HTTPException(error.status_code, {"code": error.code}) from None
    result_key = "kickResult" if task.review_kind == "KICK" else "contextResult"
    return JSONResponse(status_code=202, content={"taskId": str(task_id), result_key: result})
