"""Authenticated read preflight with controlled BE responses; no model/mutations."""
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import unittest
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from app.business_mcp.backend import BackendApi
from app.business_mcp.config import Settings
from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.integration.preflight import inspect_task
from app.main import create_app
from app.schemas import TaskEnvelope
from runtime_support import AUTH
from test_agent_read import FIXTURE, FixtureTools, local_mcp, TOKEN
from test_workflow_integration import LIVE, USER


@asynccontextmanager
async def no_runtime():
    yield None


class PreflightTests(unittest.TestCase):
    def setUp(self):
        fixture = deepcopy(FIXTURE)
        fixture["case"].update(liveId=LIVE, userId=USER, handlingRoute="CONTEXT_REVIEW")
        for message in fixture["context"]["messages"]:
            message["visibility"] = "HELD"
        self.fixture = fixture
        self.tools = FixtureTools(fixture)
        self.body = fixture["task"]
        self.url = f"/agent/tasks/{self.body['taskId']}/preflight"

    def application(self, **kwargs):
        return create_app(runtime_factory=no_runtime, service_token="test-inbound-token",
                          preflight_tools_factory=lambda: self.tools, **kwargs)

    def test_reads_can_pass_without_gateway_but_never_authorize_or_accept_execution(self):
        with TestClient(self.application()) as client:
            result = client.post(self.url, json=self.body, headers=AUTH)
            self.assertEqual(result.status_code, 200)
            report = result.json()
            self.assertTrue(report["readContractReady"])
            self.assertFalse(report["executionAuthorized"])
            self.assertFalse(client.get("/health").json()["readyForTasks"])
            self.assertEqual(client.get(self.url.removesuffix("/preflight"), headers=AUTH).status_code, 503)
        self.assertEqual(self.tools.calls, ["get_moderation_case", "get_chat_context"])
        self.assertNotIn(self.fixture["context"]["messages"][0]["text"], json.dumps(report, ensure_ascii=False))

    def test_auth_and_task_binding_reject_before_any_read(self):
        with TestClient(self.application()) as client:
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"X-Service-Token": "test-inbound-token"}):
                self.assertEqual(client.post(self.url, json=self.body, headers=headers).status_code, 401)
            changed = {**self.body, "taskId": str(uuid4())}
            self.assertEqual(client.post(self.url, json=changed, headers=AUTH).status_code, 400)
            resumed = {**self.body, "kind": "RESUME_REVIEW", "decisionId": str(uuid4()),
                       "reviewId": str(uuid4()), "reviewKind": "CONTEXT"}
            self.assertEqual(client.post(self.url, json=resumed, headers=AUTH).status_code, 400)
        self.assertEqual(self.tools.calls, [])

    def test_real_backend_not_ready_code_survives_without_context_read(self):
        class NotReady:
            calls = []
            async def call(inner, name, args):
                inner.calls.append(name)
                raise AgentToolError("CASE_NOT_READY", trace_id=args["traceId"])
        self.tools = NotReady()
        with TestClient(self.application()) as client:
            report = client.post(self.url, json=self.body, headers=AUTH).json()
        self.assertEqual(report["blockers"], [{"code": "CASE_NOT_READY", "retryable": False}])
        self.assertEqual(self.tools.calls, ["get_moderation_case"])

    def test_scope_visibility_and_snapshot_gaps_block_independently(self):
        variants = [("missing_scope", "BACKEND_CONTRACT_INCOMPLETE"),
                    ("public_target", "EXPECTED_HELD_TARGET"),
                    ("stale", "STALE_EVIDENCE"), ("unversioned", "INVALID_TOOL_RESPONSE")]
        for mode, code in variants:
            with self.subTest(mode=mode):
                fixture = deepcopy(self.fixture)
                if mode == "missing_scope": del fixture["case"]["userId"]
                if mode == "public_target":
                    for item in fixture["context"]["messages"]: item["visibility"] = "PUBLIC"
                if mode == "stale": fixture["context"]["evidenceVersion"] += 1
                if mode == "unversioned": del fixture["context"]["snapshotId"]
                self.tools = FixtureTools(fixture)
                with TestClient(self.application()) as client:
                    report = client.post(self.url, json=self.body, headers=AUTH).json()
                self.assertFalse(report["readContractReady"])
                self.assertEqual(report["blockers"][0]["code"], code)

    def test_configuration_and_unexpected_errors_do_not_expose_secrets(self):
        def bad_config():
            raise ValueError("private-token-or-url")
        application = create_app(runtime_factory=no_runtime, service_token="test-inbound-token",
                                 preflight_tools_factory=bad_config)
        with TestClient(application) as client:
            result = client.post(self.url, json=self.body, headers=AUTH)
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.json()["detail"]["code"], "BUSINESS_MCP_NOT_CONFIGURED")
            self.assertNotIn("private-token", result.text)
        class Broken:
            async def call(self, name, args):
                raise RuntimeError("private-chat-and-token")
        self.tools = Broken()
        with TestClient(self.application()) as client:
            result = client.post(self.url, json=self.body, headers=AUTH)
            self.assertEqual(result.status_code, 503)
            self.assertNotIn("private-chat", result.text)


class PreflightMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_409_is_preserved_through_http_adapter_mcp_wire_and_preflight(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers["X-Service-Token"], "fixture-service-token")
            return httpx.Response(409, json={"error": {"code": "CASE_NOT_READY", "retryable": False,
                                                     "message": "private raw chat"}})
        settings = Settings("http://controlled-backend", "fixture-service-token", TOKEN, "127.0.0.1", 8002)
        async with httpx.AsyncClient(base_url=settings.backend_base_url,
                                     transport=httpx.MockTransport(handler)) as http:
            async with local_mcp(BackendApi(settings, http)) as url:
                report = await inspect_task(TaskEnvelope.model_validate(FIXTURE["task"]),
                                            BusinessMcpReadClient(BusinessMcpSettings(url, TOKEN)))
        self.assertEqual(report["blockers"], [{"code": "CASE_NOT_READY", "retryable": False}])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertNotIn("private raw chat", json.dumps(report))
