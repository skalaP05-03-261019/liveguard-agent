"""Isolated Docker integration fixture; never connect it to a real Backend."""
import asyncio
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


app = FastAPI()
reads = []
commands = []
release = asyncio.Event()
read_release = asyncio.Event()
block_next_read = False
waiting_read = None


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.url.path.startswith("/internal/"):
        if request.headers.get("x-service-token") != os.environ["BUSINESS_MCP_SERVICE_TOKEN"]:
            return JSONResponse(status_code=401, content={"error": "UNAUTHENTICATED"})
    return await call_next(request)


@app.get("/internal/cases/{case_id}")
async def case(case_id: str, request: Request):
    global block_next_read, waiting_read
    reads.append(request.client.host)
    if block_next_read:
        block_next_read = False
        waiting_read = request.client.host
        await read_release.wait()
        waiting_read = None
    return {"data": {"caseId": case_id, "servedBy": request.client.host}}


@app.post("/internal/actions/{action_id}/execute")
async def execute(action_id: str, request: Request):
    command = await request.json()
    commands.append({"actionId": action_id, "idempotencyKey": command["idempotencyKey"],
                     "servedBy": request.client.host})
    # Record the effect before withholding the response; the test kills its MCP.
    await release.wait()
    return {"data": {"actionId": action_id, "status": "APPLIED"}}


@app.get("/internal/actions/{action_id}")
async def action(action_id: str):
    applied = any(command["actionId"] == action_id for command in commands)
    return {"data": {"actionId": action_id, "status": "APPLIED" if applied else "PENDING"}}


@app.get("/test/state")
async def state():
    return {"reads": reads, "commands": commands, "waitingRead": waiting_read}


@app.post("/test/release")
async def finish():
    release.set()
    return {"released": True}


@app.post("/test/block-read")
async def block_read():
    global block_next_read
    read_release.clear()
    block_next_read = True
    return {"blocked": True}


@app.post("/test/release-read")
async def finish_read():
    read_release.set()
    return {"released": True}
