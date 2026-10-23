"""Controlled model/policy only. Business responses always come from real Spring."""
from contextlib import asynccontextmanager
import hmac
import json
import os
import asyncio
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app.clients.mcp_logging import protect_mcp_logs
from app.rag_mcp.config import policy_file_for
from app.rag_mcp.policy import parse_policy
from app.rag_mcp.tools import register_tools


def controlled_call(messages):
    initial = json.loads(next(message["content"] for message in messages
                              if message["role"] == "user"))
    targets = initial["targetMessages"]
    if not targets or any("[JOINT96:" not in message["text"] for message in targets):
        raise ValueError("SYNTHETIC_TARGET_REQUIRED")
    if not any(message["role"] == "tool" for message in messages):
        return "get_policy_section", {"policyId": "CP-3.2"}
    findings = []
    for message in targets:
        text = message["text"]
        if "[JOINT96:NORMAL]" in text:
            verdict = "NO_VIOLATION"
        elif "[JOINT96:VIOLATION]" in text:
            verdict = "VIOLATION"
        elif "[JOINT96:DEFER]" in text:
            verdict = "NEEDS_REVIEW"
        else:
            raise ValueError("UNKNOWN_SYNTHETIC_SCENARIO")
        findings.append({
            "messageId": message["messageId"], "verdict": verdict,
            "categories": ["HARASSMENT"] if verdict == "VIOLATION" else [],
            "policyIds": ["CP-3.2"], "evidenceMessageIds": [message["messageId"]],
            "reason": "Controlled integration proposal; not a model accuracy assessment.",
            "missingFacts": ["Synthetic case requires operator confirmation."] if verdict == "NEEDS_REVIEW" else [],
        })
    return "finish_investigation", {"messageFindings": findings}


class PolicyFixture:
    def __init__(self):
        self.sections = {chunk.policy_id: chunk for chunk in parse_policy(
            policy_file_for("v3.1").read_text(), version="v3.1")}

    async def list_versions(self):
        return {"v3.1": "controlled-no-embedding"}

    async def list_policy_ids(self, version):
        return list(self.sections) if version == "v3.1" else []

    async def get_section(self, policy_id, version):
        if version != "v3.1" or policy_id not in self.sections:
            return None
        chunk = self.sections[policy_id]
        return {"policy_id": chunk.policy_id, "title": chunk.title, "text": chunk.text,
                "version": chunk.version, "category": chunk.category.value if chunk.category else None,
                "resource_uri": chunk.resource_uri}

    async def search(self, vector, *, version, category, top_k):
        # Deterministic retrieval fixture, not vector search quality evidence.
        return [{**await self.get_section("CP-3.2", version), "score": 1.0}]


class NoEmbedding:
    model = "controlled-no-embedding"

    async def embed_query(self, text):
        return [0.0]


def create_app():
    if os.environ.get("JOINT_TEST_ENABLED") != "synthetic-only":
        raise ValueError("Explicit synthetic-only opt-in required")
    token = os.environ["JOINT_TEST_TOKEN"]
    rag_token = os.environ["RAG_MCP_AGENT_TOKEN"]
    mcp = MCPServer("Joint test policy fixture")
    protect_mcp_logs()
    repository = PolicyFixture()
    register_tools(mcp, lambda: repository, NoEmbedding)
    transport = mcp.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["controlled-fixture:*", "localhost:*", "127.0.0.1:*"],
        ),
    )
    state = {"modelCalls": 0, "rateLimits": 0, "timeouts": 0, "lostResponses": 0,
             "executions": [], "actionReads": [], "fault": None}

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.test_state = state

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        path = request.url.path
        if path == "/health":
            return await call_next(request)
        if path.startswith("/backend/"):
            # Internal service credentials are validated by Spring, not replaced here.
            return await call_next(request)
        expected = rag_token if path.startswith("/mcp") else token
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            return JSONResponse(status_code=401, content={"code": "UNAUTHENTICATED"})
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"ready": True, "mode": "controlled-synthetic", "policyVersion": "v3.1"}

    @app.get("/control")
    async def metrics():
        return {key: value for key, value in state.items() if key != "fault"}

    @app.post("/control")
    async def arm(request: Request):
        body = await request.json()
        fault = body.get("fault")
        if fault not in {None, "model429", "model-timeout", "action-response-loss"}:
            return JSONResponse(status_code=400, content={"code": "INVALID_FAULT"})
        state["fault"] = fault
        return {"armed": fault}

    @app.post("/v1/chat/completions")
    async def complete(request: Request):
        body = await request.json()
        try:
            name, arguments = controlled_call(body["messages"])
        except (ValueError, KeyError, TypeError, StopIteration):
            return JSONResponse(status_code=400, content={"error": {"message": "SYNTHETIC_INPUT_REQUIRED"}})
        state["modelCalls"] += 1
        if state["fault"] == "model429":
            state["fault"] = None
            state["rateLimits"] += 1
            return JSONResponse(status_code=429, headers={"Retry-After": "1"},
                                content={"error": {"message": "CONTROLLED_RATE_LIMIT"}})
        if state["fault"] == "model-timeout":
            state["fault"] = None
            state["timeouts"] += 1
            await asyncio.sleep(35)
        return {"id": f"controlled-{uuid4()}", "object": "chat.completion", "created": 0,
                "model": "joint-controlled-v1",
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{
                        "id": str(uuid4()), "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }],
                }}], "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}}

    @app.api_route("/backend/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request):
        if not path.startswith("internal/"):
            return JSONResponse(status_code=403, content={"code": "INTERNAL_ONLY"})
        # Fixed Compose destination: this fixture cannot redirect requests to arbitrary hosts.
        async with httpx.AsyncClient(base_url="http://backend:8080", trust_env=False,
                                     timeout=10, follow_redirects=False) as http:
            response = await http.request(
                request.method, "/" + path, params=request.query_params,
                content=await request.body(),
                headers={key: value for key, value in request.headers.items()
                         if key in {"x-service-token", "x-trace-id", "content-type"}},
            )
        execute = request.method == "POST" and path.startswith("internal/actions/") and path.endswith("/execute")
        if execute:
            action_id = path.split("/")[2]
            state["executions"].append({"actionId": action_id, "httpStatus": response.status_code,
                                        "mcpIp": request.client.host})
        if request.method == "GET" and path.startswith("internal/actions/"):
            state["actionReads"].append({"actionId": path.split("/")[2], "mcpIp": request.client.host})
        if execute and response.is_success and state["fault"] == "action-response-loss":
            state["fault"] = None
            state["lostResponses"] += 1

            async def broken_response():
                yield b'{"data":'
                raise RuntimeError("CONTROLLED_RESPONSE_LOSS")

            return StreamingResponse(broken_response(), media_type="application/json")
        return JSONResponse(status_code=response.status_code, content=response.json())

    app.mount("/", transport)
    return app
