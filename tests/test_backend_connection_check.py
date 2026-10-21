"""Offline diagnostic checks; actual Spring execution is recorded separately."""

from copy import deepcopy
import os
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.check_backend_connection import inspect_connection


CASE = "30000000-0000-4000-8000-000000000003"
TOKEN = "fixture-service-token"


class BackendCheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, *, incomplete=False, stale=False, open_auth=False, http_case_status=200,
                        backend_targets=False):
        snapshot = "30000000-0000-4000-8000-000000000004"
        case = {"caseId": CASE, "snapshotId": snapshot, "evidenceVersion": 2, "policyVersion": "3.1",
                "targetMessageIds": [CASE], "targetVersions": {CASE: 1}, "liveId": snapshot,
                "userId": snapshot, "handlingRoute": "CONTEXT_REVIEW"}
        if backend_targets:
            del case["targetMessageIds"]
            del case["targetVersions"]
            case["targets"] = [{"messageId": CASE, "visibility": "HELD", "version": 1}]
        context = {"caseId": CASE, "snapshotId": snapshot, "evidenceVersion": 1 if stale else 2,
                   "messages": [{"messageId": CASE, "authorAlias": "masked", "text": "private raw chat",
                                 "sentAt": "2026-10-21T00:00:00Z", "visibility": "HELD"}]}
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/v3/api-docs":
                return httpx.Response(200, json={"paths": {"/internal/cases/{caseId}": {},
                    "/internal/actions/{id}/execute": {}}})
            if not open_auth and request.headers.get("X-Service-Token") != TOKEN:
                return httpx.Response(403)
            if request.url.path.endswith("/context"):
                return httpx.Response(200, json={"data": context})
            if request.url.path == f"/internal/cases/{CASE}":
                return httpx.Response(501 if incomplete else http_case_status, json={"data": case})
            return httpx.Response(501, json={"error": {"code": "NOT_IMPLEMENTED", "retryable": False}})
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handler))
        from app.clients.business_mcp import AgentToolError
        async def tool(name, args):
            if name == "get_chat_context":
                return {"data": context}
            if name == "get_moderation_case" and not incomplete:
                return {"data": case}
            if name in {"get_stream_info", "get_official_links"}:
                return {"data": {"title": "connected"}}
            raise AgentToolError("NOT_IMPLEMENTED", trace_id=args["traceId"])
        business = AsyncMock()
        business.call.side_effect = tool
        with patch.dict(os.environ, {"BUSINESS_BACKEND_BASE_URL": "http://backend", "BUSINESS_MCP_SERVICE_TOKEN": TOKEN}), \
             patch("app.check_backend_connection.httpx.AsyncClient", side_effect=client), \
             patch("app.check_backend_connection.BusinessMcpReadClient", return_value=business), \
             patch("app.check_backend_connection.BusinessMcpSettings.from_env"):
            report = await inspect_connection(CASE)
        self.assertFalse(any(r.method != "GET" for r in requests))
        self.assertNotIn("private raw chat", str(report))
        self.assertNotIn(TOKEN, str(report))
        return report, business

    async def test_501_does_not_abort_other_reads_or_claim_documented_route_is_implemented(self):
        report, business = await self.run_probe(incomplete=True)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertFalse(report["readContractReady"])
        self.assertTrue(report["checks"]["documentedB2Routes"]["case"])
        self.assertTrue(report["checks"]["documentedB2Routes"]["actionExecution"])
        self.assertNotIn("implementedB2Routes", report["checks"])
        self.assertEqual(report["checks"]["caseEndpoint"], 501)
        self.assertEqual(report["checks"]["mcpReads"]["get_moderation_case"]["errorCode"], "NOT_IMPLEMENTED")
        self.assertEqual(report["checks"]["mcpReads"]["get_stream_info"]["status"], "CONNECTED")
        self.assertEqual(business.call.await_count, 6)

    async def test_matching_read_contract_is_distinct_from_missing_execution_routes(self):
        report, _ = await self.run_probe()
        self.assertEqual(report["status"], "READ_CONTRACT_READY")
        self.assertTrue(report["readContractReady"])
        self.assertTrue(report["checks"]["evidenceBindingReady"])
        self.assertFalse(report["mutationExecuted"])
        self.assertTrue(report["blockers"])  # Other contracts still have 501s.

    async def test_stale_context_is_not_ready_even_when_both_schemas_are_valid(self):
        report, _ = await self.run_probe(stale=True)
        self.assertFalse(report["readContractReady"])
        self.assertIn({"stage": "evidenceBinding", "errorCode": "STALE_EVIDENCE"}, report["blockers"])

    async def test_backend_target_format_passes_real_loader_binding(self):
        report, _ = await self.run_probe(backend_targets=True)
        self.assertTrue(report["readContractReady"])
        self.assertTrue(report["checks"]["evidenceBindingReady"])

    async def test_missing_auth_enforcement_is_not_ready(self):
        report, _ = await self.run_probe(open_auth=True)
        self.assertFalse(report["readContractReady"])
        self.assertFalse(report["checks"]["authBoundaryReady"])

    async def test_successful_mcp_cannot_hide_unavailable_direct_case_endpoint(self):
        report, _ = await self.run_probe(http_case_status=501)
        self.assertFalse(report["readContractReady"])

    async def test_real_shaped_b1_response_is_partial_not_silently_normalized(self):
        context = {"messages": [{"messageId": CASE, "authorAlias": "U1", "text": "[EMAIL]",
                                 "sentAt": "2026-10-20T00:00:00Z"}], "truncated": False}
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path == "/v3/api-docs":
                return httpx.Response(200, json={"paths": {}})
            if request.headers.get("X-Service-Token") != TOKEN:
                return httpx.Response(403)
            if request.url.path.endswith("/context"):
                return httpx.Response(200, json={"data": deepcopy(context)})
            return httpx.Response(404)
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handler))
        business = AsyncMock()
        business.call.return_value = {"data": context}
        with patch.dict(os.environ, {"BUSINESS_BACKEND_BASE_URL": "http://backend", "BUSINESS_MCP_SERVICE_TOKEN": TOKEN}), \
             patch("app.check_backend_connection.httpx.AsyncClient", side_effect=client), \
             patch("app.check_backend_connection.BusinessMcpReadClient", return_value=business), \
             patch("app.check_backend_connection.BusinessMcpSettings.from_env"):
            report = await inspect_connection(CASE)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertFalse(report["readContractReady"])
        self.assertTrue(report["backendHttpObserved"])
        self.assertIn("snapshotId", report["checks"]["mcpContextSchemaGaps"])
        self.assertIn("messages.0.visibility", report["checks"]["mcpContextSchemaGaps"])
        self.assertFalse(any(r.method == "POST" for r in requests))
        self.assertNotIn(TOKEN, str(report))

    async def test_unreachable_backend_is_not_marked_observed(self):
        def handler(request):
            raise httpx.ConnectError("synthetic", request=request)
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(**kwargs, transport=httpx.MockTransport(handler))
        with patch.dict(os.environ, {"BUSINESS_BACKEND_BASE_URL": "http://backend", "BUSINESS_MCP_SERVICE_TOKEN": TOKEN}), \
             patch("app.check_backend_connection.httpx.AsyncClient", side_effect=client):
            report = await inspect_connection(CASE)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertFalse(report["backendHttpObserved"])
