import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import ValidationError
import uvicorn
import httpx2

from app.business_mcp.config import Settings
from app.business_mcp.main import create_app
from app.business_mcp.schemas import BackendError
from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.main import create_app as create_agent_app
from app.nodes import load_case
from app.schemas import AnalysisResult, TaskEnvelope


FIXTURE = json.loads((Path(__file__).parent / "fixtures/agent-case-v0.5.json").read_text())
TOKEN = "local-test-agent-token"


class FixtureBackend:
    """Controlled business dependency; all MCP transport and authentication are real."""

    def __init__(self, fixture=None, *, error=False, delay=0):
        self.fixture = deepcopy(fixture or FIXTURE)
        self.calls = []
        self.error = error
        self.delay = delay

    async def get_case_context(self, case_id, *, view, trace_id, params=None):
        self.calls.append((str(case_id), view, trace_id, params))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise BackendError("STALE_EVIDENCE", "Evidence changed", retryable=False, trace_id=trace_id)
        return self.fixture["case" if view == "moderationCase" else "context"]


@contextmanager
def tcp_mcp(backend):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
    except OSError:
        sock.close()
        raise
    port = sock.getsockname()[1]
    settings = Settings("http://unused.test", "test-service-token", TOKEN, "127.0.0.1", port)
    server = uvicorn.Server(uvicorn.Config(
        create_app(settings, backend=backend), log_level="critical", access_log=False,
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("Local MCP server did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Local MCP server did not stop")


@asynccontextmanager
async def local_mcp(backend):
    if os.getenv("LIVEGUARD_TEST_TCP") == "1":
        with tcp_mcp(backend) as url:
            yield url
        return
    # Exercise the same ASGI app, auth, MCP framing and SDK client without a TCP port.
    # Enable LIVEGUARD_TEST_TCP=1 where local socket binding is allowed.
    settings = Settings("http://unused.test", "test-service-token", TOKEN, "127.0.0.1", 8002)
    application = create_app(settings, backend=backend)

    def http_client_factory(*, headers):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application), headers=headers)

    async with application.router.lifespan_context(application):
        with patch("app.clients.business_mcp.create_mcp_http_client", side_effect=http_client_factory):
            yield "http://127.0.0.1:8002/mcp"


class FixtureTools:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append(name)
        key = "case" if name == "get_moderation_case" else "context"
        return {"data": self.fixture[key], "traceId": arguments["traceId"]}


class ContractTests(unittest.TestCase):
    def test_new_input_and_fail_closed_api_status(self):
        task = FIXTURE["task"]
        self.assertEqual(TaskEnvelope.model_validate(task).snapshot_id, UUID(task["snapshotId"]))
        with TestClient(create_agent_app(service_token="contract-test"), headers={"Authorization": "Bearer contract-test"}) as client:
            self.assertEqual(client.post(f'/agent/tasks/{task["taskId"]}/run', json=task).status_code, 503)
            wrong_url = "00000000-0000-4000-8000-000000000099"
            self.assertEqual(client.post(f"/agent/tasks/{wrong_url}/run", json=task).status_code, 400)
            old = {**task, "expectedCaseVersion": 1}
            del old["snapshotId"]
            del old["expectedEvidenceVersion"]
            self.assertEqual(client.post(f'/agent/tasks/{task["taskId"]}/run', json=old).status_code, 422)
            review = {**task, "kind": "RESUME_REVIEW"}
            self.assertEqual(client.post(f'/agent/tasks/{task["taskId"]}/resume', json=review).status_code, 422)
            review["decisionId"] = wrong_url
            self.assertEqual(client.post(f'/agent/tasks/{task["taskId"]}/resume', json=review).status_code, 400)
            self.assertEqual(client.post(f'/agent/tasks/{task["taskId"]}/run', json=review).status_code, 400)
            self.assertEqual(client.post(f'/agent/tasks/{task["taskId"]}/resume', json=task).status_code, 400)

    def test_output_is_per_message_and_cannot_set_duration(self):
        finding = {
            "messageId": FIXTURE["case"]["targetMessageIds"][0], "verdict": "VIOLATION",
            "categories": ["PROFANITY", "HARASSMENT"], "policyIds": ["CP-2.1"],
            "evidenceMessageIds": FIXTURE["case"]["targetMessageIds"], "reason": "test evidence",
        }
        result = {
            "caseId": FIXTURE["task"]["caseId"], "snapshotId": FIXTURE["task"]["snapshotId"],
            "evidenceVersion": 1, "policyVersion": "fixture-policy-v1", "messageFindings": [finding],
        }
        self.assertEqual(len(AnalysisResult.model_validate(result).message_findings), 1)
        for bad in [
            {**result, "durationSeconds": 600},
            {**result, "messageFindings": [finding, finding]},
            {**result, "messageFindings": [{**finding, "durationSeconds": 600}]},
            {**result, "messageFindings": [{**finding, "evidenceMessageIds": []}]},
            {**result, "messageFindings": [{**finding, "categories": ["PROFANITY", "PROFANITY"]}]},
        ]:
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                AnalysisResult.model_validate(bad)
        for verdict in ["NO_VIOLATION", "NEEDS_REVIEW"]:
            safe = {**finding, "verdict": verdict, "categories": [], "policyIds": [],
                    "missingFacts": ["context unavailable"] if verdict == "NEEDS_REVIEW" else []}
            AnalysisResult.model_validate({**result, "messageFindings": [safe]})
        no_violation_with_policy = {
            **finding, "verdict": "NO_VIOLATION", "categories": [],
            "policyIds": ["CP-2.1"], "evidenceMessageIds": [],
        }
        AnalysisResult.model_validate({
            **result, "messageFindings": [no_violation_with_policy],
        })
        no_violation_with_category = {
            **no_violation_with_policy, "categories": ["PROFANITY"],
        }
        with self.assertRaises(ValidationError):
            AnalysisResult.model_validate({
                **result, "messageFindings": [no_violation_with_category],
            })

    def test_configuration_has_no_secret_repr(self):
        with patch.dict("os.environ", {"BUSINESS_MCP_URL": "http://localhost:8002/mcp",
                                       "BUSINESS_MCP_AGENT_TOKEN": TOKEN}):
            self.assertNotIn(TOKEN, repr(BusinessMcpSettings.from_env()))
        for url, token in [("file:///tmp/mcp", TOKEN), ("http://user:pass@localhost/mcp", TOKEN),
                           ("http://localhost/mcp?token=secret", TOKEN), ("http://localhost/mcp", "")]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                BusinessMcpSettings(url, token)


class LoaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_or_changed_evidence_stops_loading(self):
        for mode, expected in [
            ("case_id", "INVALID_EVIDENCE"), ("snapshot", "STALE_EVIDENCE"),
            ("version", "STALE_EVIDENCE"), ("missing_case_field", "INVALID_TOOL_RESPONSE"),
            ("missing_target", "INVALID_EVIDENCE"), ("duplicate_message", "INVALID_EVIDENCE"),
            ("context_version", "STALE_EVIDENCE"), ("context_snapshot", "STALE_EVIDENCE"),
            ("context_case", "INVALID_EVIDENCE"), ("missing_context_version", "INVALID_TOOL_RESPONSE"),
        ]:
            fixture = deepcopy(FIXTURE)
            other = "00000000-0000-4000-8000-000000000099"
            if mode == "case_id": fixture["case"]["caseId"] = other
            elif mode == "snapshot": fixture["case"]["snapshotId"] = other
            elif mode == "version": fixture["case"]["evidenceVersion"] = 2
            elif mode == "missing_case_field": del fixture["case"]["targetVersions"]
            elif mode == "missing_target": fixture["context"]["messages"] = fixture["context"]["messages"][1:]
            elif mode == "duplicate_message": fixture["context"]["messages"] *= 2
            elif mode == "context_version": fixture["context"]["evidenceVersion"] = 2
            elif mode == "context_snapshot": fixture["context"]["snapshotId"] = other
            elif mode == "context_case": fixture["context"]["caseId"] = other
            elif mode == "missing_context_version": del fixture["context"]["evidenceVersion"]
            tools = FixtureTools(fixture)
            with self.subTest(mode=mode), self.assertRaises(AgentToolError) as raised:
                await load_case({"task": TaskEnvelope.model_validate(FIXTURE["task"])}, tools=tools)
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn(FIXTURE["context"]["messages"][0]["text"], str(raised.exception))
            if mode in {"case_id", "snapshot", "version", "missing_case_field"}:
                self.assertEqual(tools.calls, ["get_moderation_case"])


class McpHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_http_loads_case_and_preserves_trace(self):
        backend = FixtureBackend()
        async with local_mcp(backend) as url:
            with patch.dict("os.environ", {"BUSINESS_MCP_URL": url, "BUSINESS_MCP_AGENT_TOKEN": TOKEN}):
                state = await load_case({"task": TaskEnvelope.model_validate(FIXTURE["task"])})
        self.assertEqual(state["snapshot_id"], FIXTURE["task"]["snapshotId"])
        self.assertEqual(state["evidence_version"], 1)
        self.assertEqual(len(state["target_messages"]), 1)
        self.assertEqual(len(state["context_messages"]), 1)
        self.assertEqual(len(backend.calls), 2)
        self.assertTrue(all(call[2] == FIXTURE["task"]["traceId"] for call in backend.calls))
        self.assertEqual(backend.calls[1][3], {"before": 5, "after": 5})
        self.assertNotIn(TOKEN, json.dumps(state["tool_observations"]))
        self.assertNotIn("무대 미쳤다", json.dumps(state["tool_observations"], ensure_ascii=False))

    async def test_wrong_token_is_explicit_and_does_not_query_backend(self):
        backend = FixtureBackend()
        async with local_mcp(backend) as url:
            client = BusinessMcpReadClient(BusinessMcpSettings(url, "incorrect-token"))
            with self.assertRaises(AgentToolError) as raised:
                await client.call("get_moderation_case", self.arguments())
        self.assertEqual(raised.exception.code, "UNAUTHENTICATED")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(backend.calls, [])
        self.assertNotIn("incorrect-token", str(raised.exception))

    async def test_backend_tool_error_is_not_a_success_or_retry(self):
        backend = FixtureBackend(error=True)
        async with local_mcp(backend) as url:
            client = BusinessMcpReadClient(BusinessMcpSettings(url, TOKEN))
            with self.assertRaises(AgentToolError) as raised:
                await client.call("get_moderation_case", self.arguments())
        self.assertEqual(raised.exception.code, "STALE_EVIDENCE")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(backend.calls), 1)

    async def test_invalid_wire_data_is_not_treated_as_case_data(self):
        fixture = deepcopy(FIXTURE)
        fixture["case"] = []
        backend = FixtureBackend(fixture)
        async with local_mcp(backend) as url:
            client = BusinessMcpReadClient(BusinessMcpSettings(url, TOKEN))
            with self.assertRaises(AgentToolError) as raised:
                await client.call("get_moderation_case", self.arguments())
        self.assertEqual(raised.exception.code, "INVALID_TOOL_RESPONSE")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(backend.calls), 1)

    async def test_timeout_is_bounded_and_not_retried(self):
        backend = FixtureBackend(delay=0.5)
        async with local_mcp(backend) as url:
            client = BusinessMcpReadClient(BusinessMcpSettings(url, TOKEN, timeout_seconds=0.15))
            with self.assertRaises(AgentToolError) as raised:
                await client.call("get_moderation_case", self.arguments())
        self.assertEqual(raised.exception.code, "TEMPORARY_UNAVAILABLE")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(backend.calls), 1)

    async def test_mutations_and_missing_trace_are_blocked_before_network(self):
        client = BusinessMcpReadClient(BusinessMcpSettings("http://127.0.0.1:1/mcp", TOKEN))
        for name, args, code in [
            ("hide_message", self.arguments(), "FORBIDDEN"),
            ("timeout_user", self.arguments(), "FORBIDDEN"),
            ("kick_user", self.arguments(), "FORBIDDEN"),
            ("get_moderation_case", {}, "INVALID_INPUT"),
        ]:
            with self.subTest(name=name), self.assertRaises(AgentToolError) as raised:
                await client.call(name, args)
            self.assertEqual(raised.exception.code, code)

    @staticmethod
    def arguments():
        return {"caseId": FIXTURE["task"]["caseId"], "traceId": FIXTURE["task"]["traceId"]}


if __name__ == "__main__":
    unittest.main()
