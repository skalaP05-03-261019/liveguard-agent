"""Compose Host regression; real SDK/ASGI or TCP, controlled Backend, no Docker DNS claim."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from app.business_mcp.config import DEFAULT_ALLOWED_HOSTS, Settings
from app.clients import business_mcp
from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings
from test_agent_read import FIXTURE, FixtureBackend, local_mcp, TOKEN


class HostSettingsTests(unittest.TestCase):
    def test_missing_env_and_example_agree_on_compose_and_loopback_hosts(self):
        with patch.dict(os.environ):
            os.environ.pop("BUSINESS_MCP_ALLOWED_HOSTS", None)
            self.assertEqual(Settings.from_env().allowed_hosts, DEFAULT_ALLOWED_HOSTS)
        self.assertEqual(set(DEFAULT_ALLOWED_HOSTS), {"business-mcp:*", "localhost:*", "127.0.0.1:*"})
        example = (Path(__file__).parents[1] / ".env.example").read_text()
        value = next(line.split("=", 1)[1] for line in example.splitlines()
                     if line.startswith("BUSINESS_MCP_ALLOWED_HOSTS="))
        self.assertEqual(tuple(value.split(",")), DEFAULT_ALLOWED_HOSTS)

    def test_explicit_env_replaces_default_instead_of_adding_unwanted_hosts(self):
        with patch.dict(os.environ, {"BUSINESS_MCP_ALLOWED_HOSTS": " renamed-service:8002 , localhost:* "}):
            self.assertEqual(Settings.from_env().allowed_hosts, ("renamed-service:8002", "localhost:*"))

    def test_empty_or_global_wildcard_list_fails_before_serving_requests(self):
        for value in ("", " , ", "*", "localhost:*,*"):
            with self.subTest(value=value), patch.dict(os.environ, {"BUSINESS_MCP_ALLOWED_HOSTS": value}):
                with self.assertRaises(ValueError):
                    Settings.from_env()


class ComposeHostToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_accepts_compose_host_on_real_sdk_transport(self):
        backend = FixtureBackend()
        async with local_mcp(backend) as url:
            original = business_mcp.create_mcp_http_client
            def compose_host(*, headers):
                return original(headers={**headers, "Host": "business-mcp:8002"})
            tools = BusinessMcpReadClient(BusinessMcpSettings(url, TOKEN), http_client_factory=compose_host)
            result = await tools.call("get_moderation_case", {
                "caseId": FIXTURE["task"]["caseId"], "traceId": FIXTURE["task"]["traceId"]})
        self.assertEqual(result["data"]["caseId"], FIXTURE["task"]["caseId"])
        self.assertEqual(len(backend.calls), 1)
