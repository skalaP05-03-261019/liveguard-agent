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
