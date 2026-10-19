"""API 경계만 제공한다. 작업 저장·실행 연결 전에는 202로 수락하지 않는다."""

from uuid import UUID

from fastapi import FastAPI, HTTPException

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


@app.post("/agent/tasks/{task_id}/resume", responses={501: {"description": "Scaffold only"}})
async def resume_task(task_id: UUID, task: TaskEnvelope):
    if task_id != task.task_id:
        raise HTTPException(400, "URL taskId and body taskId must match")
    if task.kind != "RESUME_REVIEW":
        raise HTTPException(400, "Expected RESUME_REVIEW")
    # TODO: Backend의 권한·저장 결정 확인 후 같은 thread로 재개.
    raise HTTPException(501, "Checkpoint and review resume are not connected")
