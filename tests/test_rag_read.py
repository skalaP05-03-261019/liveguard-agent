"""Consumer contract tests. The MCP app is a test double, not AI2's RAG server."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
import hmac
import json
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
import uvicorn

from app.clients.business_mcp import AgentToolError
from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.policy_evidence import retrieve_policy_evidence


FIXTURE = json.loads((Path(__file__).parent / "fixtures/rag-policy-contract.json").read_text())
TRACE = "00000000-0000-4000-8000-000000000008"
TOKEN = "local-test-rag-only-token"


class PolicyFixture:
    def __init__(self):
        self.section = deepcopy(FIXTURE["section"])
        self.matches = [{**deepcopy(self.section), "score": FIXTURE["score"]}]
        self.resource_text = self.section["text"]
        self.calls = []
        self.error = None
        self.delay = 0
        self.wrong_trace = False

    async def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise AgentToolError(self.error, trace_id=arguments["traceId"])
        data = {"matches": deepcopy(self.matches)} if name == "search_policy" else deepcopy(self.section)
        return {"data": data, "traceId": "wrong" if self.wrong_trace else arguments["traceId"]}

    async def read_resource(self, uri, *, policy_version, trace_id):
        self.calls.append(("resource", uri))
        return {"data": {"resourceUri": uri, "text": self.resource_text}, "traceId": trace_id}


def fixture_app(fixture, *, resource_mime="text/plain", auth_status=401):
    """Only exercise published #4 shapes; no embedding/indexing/search implementation."""
    mcp = MCPServer("Consumer RAG fixture, not production")
    app = FastAPI()

    @asynccontextmanager
    async def lifespan(_):
        async with mcp.session_manager.run():
            yield
    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {TOKEN}"):
            return JSONResponse(status_code=auth_status, content={"error": {"code": "UNAUTHENTICATED"}})
        return await call_next(request)

    @mcp.tool()
    async def search_policy(query: str, policyVersion: str, traceId: str,
                            topK: int = 4, category: str | None = None) -> dict[str, Any]:
        try:
            return await fixture.call("search_policy", {
                "query": query, "policyVersion": policyVersion, "traceId": traceId,
                "topK": topK, **({"category": category} if category is not None else {}),
            })
        except AgentToolError as error:
            raise ToolError(json.dumps({"error": {"code": error.code, "retryable": False}})) from None

    @mcp.tool()
    async def get_policy_section(policyId: str, policyVersion: str, traceId: str) -> dict[str, Any]:
        return await fixture.call("get_policy_section", {
            "policyId": policyId, "policyVersion": policyVersion, "traceId": traceId,
        })

    @mcp.resource("policy://{version}/{documentId}/{sectionId}", mime_type=resource_mime)
    async def policy(version: str, documentId: str, sectionId: str) -> str:
        fixture.calls.append(("resource", f"policy://{version}/{documentId}/{sectionId}"))
        return fixture.resource_text

    app.mount("/", mcp.streamable_http_app())
    return app


@contextmanager
def tcp_fixture(app):
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
    except OSError:
        sock.close()
        raise
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("Consumer test MCP app did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Consumer test MCP app did not stop")


@asynccontextmanager
async def local_rag(fixture, **kwargs):
    app = fixture_app(fixture, **kwargs)
    if os.getenv("LIVEGUARD_TEST_TCP") == "1":
        with tcp_fixture(app) as url:
            yield url
        return

    def factory(*, headers):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)
    async with app.router.lifespan_context(app):
        with patch("app.clients.rag_mcp.create_mcp_http_client", side_effect=factory):
            yield "http://127.0.0.1:8003/mcp"


def state():
    return {"policy_version": FIXTURE["version"], "trace_id": TRACE,
            "tool_observations": [{"toolName": "get_moderation_case", "traceId": TRACE}]}


def arguments():
    return {"query": "정책 테스트 질의", "policyVersion": FIXTURE["version"], "traceId": TRACE}


class RagConfigTests(unittest.TestCase):
    def test_separate_credentials_and_no_guessed_endpoint_or_version(self):
        with patch.dict(os.environ, {"RAG_MCP_URL": "http://localhost:8003/mcp",
                                    "RAG_MCP_AGENT_TOKEN": TOKEN,
                                    "BUSINESS_MCP_AGENT_TOKEN": "different-business-token"}):
            settings = RagMcpSettings.from_env()
            self.assertEqual(settings.token, TOKEN)
            self.assertNotIn(TOKEN, repr(settings))
        with patch.dict(os.environ, {"RAG_MCP_URL": "", "RAG_MCP_AGENT_TOKEN": "",
                                    "BUSINESS_MCP_AGENT_TOKEN": TOKEN}):
            with self.assertRaises(ValueError):
                RagMcpSettings.from_env()

    def test_invalid_configuration_is_rejected(self):
        for url, token, timeout in [
            ("file:///tmp/policy", TOKEN, 10), ("http://user:pass@localhost/mcp", TOKEN, 10),
            ("http://localhost/mcp?token=sensitive", TOKEN, 10),
            ("http://localhost/mcp#fragment", TOKEN, 10),
            ("http://localhost/mcp", "", 10), ("http://localhost/mcp", "bad\nheader", 10),
            ("http://localhost/mcp", TOKEN, 0), ("http://localhost/mcp", TOKEN, float("nan")),
        ]:
            with self.subTest(url=url, timeout=timeout), self.assertRaises(ValueError):
                RagMcpSettings(url, token, timeout)


class EvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_evidence_and_safe_observations_are_atomic(self):
        fixture = PolicyFixture()
        initial = state()
        before = deepcopy(initial)
        result = await retrieve_policy_evidence(initial, query="정책 테스트 질의", tools=fixture)
        self.assertEqual(initial, before)
        self.assertEqual(result["policy_evidence"], [FIXTURE["section"]])
        self.assertEqual([name for name, _ in fixture.calls], ["search_policy", "get_policy_section", "resource"])
        self.assertEqual(len(result["tool_observations"]), 4)
        serialized = json.dumps(result["tool_observations"], ensure_ascii=False)
        self.assertNotIn(FIXTURE["section"]["text"], serialized)
        self.assertNotIn("정책 테스트 질의", serialized)
        self.assertNotIn(TOKEN, serialized)

    async def test_search_excerpt_is_verified_against_complete_original(self):
        fixture = PolicyFixture()
        fixture.matches[0]["text"] = "문맥과 정상 예외를 함께 확인합니다."
        result = await retrieve_policy_evidence(state(), query="정책", tools=fixture)
        self.assertEqual(result["policy_evidence"][0]["text"], FIXTURE["section"]["text"])

    async def test_empty_search_is_not_a_normal_verdict_and_is_not_retried(self):
        fixture = PolicyFixture()
        fixture.matches = []
        with self.assertRaises(AgentToolError) as raised:
            await retrieve_policy_evidence(state(), query="정책", tools=fixture)
        self.assertEqual(raised.exception.code, "NO_POLICY_EVIDENCE")
        self.assertEqual(len(fixture.calls), 1)

    async def test_versions_ids_text_and_uri_must_agree(self):
        for mode, code in [
            ("search_version", "POLICY_MISMATCH"), ("section_version", "POLICY_MISMATCH"),
            ("uri_version", "POLICY_MISMATCH"), ("section_id", "INVALID_POLICY_EVIDENCE"),
            ("section_uri", "INVALID_POLICY_EVIDENCE"), ("search_text", "INVALID_POLICY_EVIDENCE"),
            ("resource_text", "INVALID_POLICY_EVIDENCE"), ("duplicate", "INVALID_POLICY_EVIDENCE"),
            ("foreign_uri", "INVALID_TOOL_RESPONSE"), ("traversal", "INVALID_TOOL_RESPONSE"),
            ("wrong_trace", "INVALID_TOOL_RESPONSE"), ("no_text", "INVALID_TOOL_RESPONSE"),
            ("invalid_score", "INVALID_TOOL_RESPONSE"), ("too_many", "INVALID_TOOL_RESPONSE"),
        ]:
            fixture = PolicyFixture()
            if mode == "search_version": fixture.matches[0]["version"] = "different"
            elif mode == "section_version": fixture.section["version"] = "different"
            elif mode == "uri_version": fixture.matches[0]["resourceUri"] = "policy://different/CP/2.1"
            elif mode == "section_id": fixture.section["policyId"] = "CP-2.2"
            elif mode == "section_uri": fixture.section["resourceUri"] = "policy://fixture-policy-v1/CP/2.2"
            elif mode == "search_text": fixture.matches[0]["text"] = "invented excerpt"
            elif mode == "resource_text": fixture.resource_text = "different original"
            elif mode == "duplicate": fixture.matches *= 2
            elif mode == "foreign_uri": fixture.matches[0]["resourceUri"] = "https://external.test/policy"
            elif mode == "traversal": fixture.matches[0]["resourceUri"] = "policy://fixture-policy-v1/CP/%2e%2e"
            elif mode == "wrong_trace": fixture.wrong_trace = True
            elif mode == "no_text": del fixture.section["text"]
            elif mode == "invalid_score": fixture.matches[0]["score"] = float("nan")
            elif mode == "too_many": fixture.matches *= 5
            initial = state()
            with self.subTest(mode=mode), self.assertRaises(AgentToolError) as raised:
                await retrieve_policy_evidence(initial, query="정책", tools=fixture)
            self.assertEqual(raised.exception.code, code, mode)
            self.assertNotIn("policy_evidence", initial)

    async def test_existing_evidence_cannot_silently_change_at_same_version(self):
        initial = state()
        initial["policy_evidence"] = [{**deepcopy(FIXTURE["section"]), "text": "older different text"}]
        before = deepcopy(initial)
        with self.assertRaises(AgentToolError) as raised:
            await retrieve_policy_evidence(initial, query="정책", tools=PolicyFixture())
        self.assertEqual(raised.exception.code, "POLICY_MISMATCH")
        self.assertEqual(initial, before)

    async def test_later_match_failure_does_not_publish_partial_evidence(self):
        fixture = PolicyFixture()
        fixture.matches.append({**deepcopy(fixture.matches[0]), "policyId": "CP-2.2",
                                "resourceUri": "policy://fixture-policy-v1/CP/2.2"})
        initial = state()
        before = deepcopy(initial)
        with self.assertRaises(AgentToolError) as raised:
            await retrieve_policy_evidence(initial, query="정책", tools=fixture)
        self.assertEqual(raised.exception.code, "INVALID_POLICY_EVIDENCE")
        self.assertEqual(initial, before)
        self.assertEqual([name for name, _ in fixture.calls],
                         ["search_policy", "get_policy_section", "resource", "get_policy_section"])

    async def test_repeated_verified_read_deduplicates_evidence(self):
        initial = state()
        patch_state = await retrieve_policy_evidence(initial, query="정책", tools=PolicyFixture())
        combined = {**initial, **patch_state}
        second = await retrieve_policy_evidence(combined, query="추가 정책 질의", tools=PolicyFixture())
        self.assertEqual(len(second["policy_evidence"]), 1)
        self.assertEqual(second["policy_evidence"], patch_state["policy_evidence"])

    async def test_invalid_arguments_never_reach_tools(self):
        for kwargs in [{"query": ""}, {"query": " "}, {"query": "x" * 1001},
                       {"query": "q", "top_k": 0}, {"query": "q", "top_k": 5},
                       {"query": "q", "category": "UNSUPPORTED"}]:
            fixture = PolicyFixture()
            with self.subTest(kwargs=kwargs), self.assertRaises(AgentToolError) as raised:
                await retrieve_policy_evidence(state(), tools=fixture, **kwargs)
            self.assertEqual(raised.exception.code, "INVALID_INPUT")
            self.assertFalse(fixture.calls)


class RagHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticated_search_section_resource_round_trip(self):
        fixture = PolicyFixture()
        async with local_rag(fixture) as url:
            with patch.dict(os.environ, {"RAG_MCP_URL": url, "RAG_MCP_AGENT_TOKEN": TOKEN}):
                result = await retrieve_policy_evidence(state(), query="정책", category="PROFANITY")
        self.assertEqual(result["policy_evidence"], [FIXTURE["section"]])
        self.assertEqual(fixture.calls[0][1]["category"], "PROFANITY")
        self.assertEqual(fixture.calls[0][1]["policyVersion"], FIXTURE["version"])
        self.assertEqual([n for n, _ in fixture.calls], ["search_policy", "get_policy_section", "resource"])

    async def test_wrong_token_and_forbidden_are_explicit_for_tools_and_resources(self):
        for status, code in [(401, "UNAUTHENTICATED"), (403, "FORBIDDEN")]:
            for resource in [False, True]:
                fixture = PolicyFixture()
                async with local_rag(fixture, auth_status=status) as url:
                    client = RagMcpReadClient(RagMcpSettings(url, "wrong-rag-token"))
                    with self.subTest(status=status, resource=resource), self.assertRaises(AgentToolError) as raised:
                        if resource:
                            await client.read_resource(FIXTURE["section"]["resourceUri"],
                                                       policy_version=FIXTURE["version"], trace_id=TRACE)
                        else:
                            await client.call("search_policy", arguments())
                self.assertEqual(raised.exception.code, code)
                self.assertFalse(raised.exception.retryable)
                self.assertFalse(fixture.calls)
                self.assertNotIn("wrong-rag-token", str(raised.exception))

    async def test_missing_policy_error_is_preserved_without_retry(self):
        fixture = PolicyFixture()
        fixture.error = "POLICY_NOT_FOUND"
        async with local_rag(fixture) as url:
            client = RagMcpReadClient(RagMcpSettings(url, TOKEN))
            with self.assertRaises(AgentToolError) as raised:
                await client.call("search_policy", arguments())
        self.assertEqual(raised.exception.code, "POLICY_NOT_FOUND")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(fixture.calls), 1)

    async def test_bad_wire_response_or_version_is_rejected(self):
        for mode, code in [("trace", "INVALID_TOOL_RESPONSE"), ("version", "POLICY_MISMATCH"),
                           ("text", "INVALID_TOOL_RESPONSE")]:
            fixture = PolicyFixture()
            if mode == "trace": fixture.wrong_trace = True
            elif mode == "version": fixture.matches[0]["version"] = "wrong"
            else: fixture.matches[0]["text"] = 15
            async with local_rag(fixture) as url:
                client = RagMcpReadClient(RagMcpSettings(url, TOKEN))
                with self.subTest(mode=mode), self.assertRaises(AgentToolError) as raised:
                    await client.call("search_policy", arguments())
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(len(fixture.calls), 1)

    async def test_timeout_is_bounded_and_not_retried(self):
        fixture = PolicyFixture()
        fixture.delay = 0.5
        async with local_rag(fixture) as url:
            client = RagMcpReadClient(RagMcpSettings(url, TOKEN, timeout_seconds=0.15))
            with self.assertRaises(AgentToolError) as raised:
                await client.call("search_policy", arguments())
        self.assertEqual(raised.exception.code, "TEMPORARY_UNAVAILABLE")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(len(fixture.calls), 1)

    async def test_resource_format_drift_and_mismatch_are_not_silent(self):
        for mode, code in [("mime", "INVALID_TOOL_RESPONSE"), ("text", "INVALID_POLICY_EVIDENCE")]:
            fixture = PolicyFixture()
            if mode == "text": fixture.resource_text = "wrong original"
            async with local_rag(fixture, resource_mime="application/json" if mode == "mime" else "text/plain") as url:
                client = RagMcpReadClient(RagMcpSettings(url, TOKEN))
                with self.subTest(mode=mode), self.assertRaises(AgentToolError) as raised:
                    await retrieve_policy_evidence(state(), query="정책", tools=client)
            self.assertEqual(raised.exception.code, code)

    async def test_non_rag_tools_and_unsafe_resource_uris_never_connect(self):
        client = RagMcpReadClient(RagMcpSettings("http://127.0.0.1:1/mcp", TOKEN))
        for name, args, code in [
            ("kick_user", arguments(), "FORBIDDEN"),
            ("get_moderation_case", arguments(), "FORBIDDEN"),
            ("search_policy", {}, "INVALID_INPUT"),
            ("search_policy", {**arguments(), "topK": True}, "INVALID_INPUT"),
            ("search_policy", {**arguments(), "extra": "field"}, "INVALID_INPUT"),
        ]:
            with self.subTest(name=name), self.assertRaises(AgentToolError) as raised:
                await client.call(name, args)
            self.assertEqual(raised.exception.code, code)
        for uri in ["https://external.test", "file:///etc/passwd", "policy://v/CP/2.1?q=secret",
                    "policy://fixture-policy-v1/CP/%2Fetc", "policy://fixture-policy-v1/CP/%252e%252e"]:
            with self.subTest(uri=uri), self.assertRaises(AgentToolError) as raised:
                await client.read_resource(uri, policy_version=FIXTURE["version"], trace_id=TRACE)
            self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
