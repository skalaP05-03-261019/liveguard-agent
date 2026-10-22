import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import io
import json
import logging
import signal
import unittest
from unittest.mock import patch

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.business_mcp.backend import BackendApi
from app.business_mcp.__main__ import DrainingServer
from app.business_mcp.config import Settings
from app.business_mcp.main import create_app
from app.business_mcp.schemas import BackendError, KickCommand
from app.clients.business_mcp import (
    AgentToolError, BusinessMcpReadClient, BusinessMcpSettings, call_business_tool,
    create_mcp_http_client,
)
from app.integration.http_gateway import HttpModerationGateway
from test_business_mcp import CASE_ID, TRACE_ID, FakeBackend
import uvicorn


TOKEN = "cluster-agent-secret"
SETTINGS = Settings("http://backend.test", "cluster-service-secret", TOKEN, "127.0.0.1", 8002)
CLIENT = BusinessMcpSettings("http://localhost:8002/mcp", TOKEN)
ARGS = {"caseId": str(CASE_ID), "traceId": str(TRACE_ID)}
COMMAND = {
    "actionId": "00000000-0000-4000-8000-000000000010",
    "planId": "00000000-0000-4000-8000-000000000011",
    "caseId": str(CASE_ID), "type": "KICK",
    "targetId": "00000000-0000-4000-8000-000000000012",
    "expectedEvidenceVersion": 4, "expectedTargetVersion": 2, "policyVersion": "0.4-review",
    "idempotencyKey": "00000000-0000-4000-8000-000000000010",
    "decisionId": "00000000-0000-4000-8000-000000000013",
    "violationIds": [
        "00000000-0000-4000-8000-000000000020",
        "00000000-0000-4000-8000-000000000021",
        "00000000-0000-4000-8000-000000000022",
    ],
}


class Cluster:
    """Request-level round robin with readiness probes, not a production proxy."""
    def __init__(self, apps):
        self.apps = apps
        self.next = 0
        self.routed = []
        self.session_ids = []
        self.break_transport = False

    async def __call__(self, scope, receive, send):
        if self.break_transport:
            raise httpx2.ReadError("private-transport-detail")
        healthy = []
        for index, app in enumerate(self.apps):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
                if (await client.get("http://localhost/ready")).status_code == 200:
                    healthy.append(index)
        if not healthy:
            await send({"type": "http.response.start", "status": 503, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        index = healthy[self.next % len(healthy)]
        self.next += 1
        self.routed.append(index)
        self.session_ids.append(dict(scope["headers"]).get(b"mcp-session-id"))
        await self.apps[index](scope, receive, send)

    def http(self, *, headers):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self), headers=headers)


class ClusterTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_before_start_and_signal_drain(self):
        app = create_app(SETTINGS, backend=FakeBackend())
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as http:
            self.assertEqual((await http.get("http://localhost/ready")).status_code, 503)
            async with app.router.lifespan_context(app):
                self.assertEqual((await http.get("http://localhost/ready")).status_code, 200)
                server = DrainingServer(uvicorn.Config(app, access_log=False))
                server.handle_exit(signal.SIGTERM, None)
                self.assertTrue(server.should_exit)
                self.assertEqual((await http.get("http://localhost/ready")).status_code, 503)
                self.assertEqual((await http.post("http://localhost/mcp")).status_code, 503)
            self.assertEqual((await http.get("http://localhost/ready")).status_code, 503)

    async def test_one_session_crosses_instances_and_new_calls_survive_drain(self):
        apps = [create_app(SETTINGS, backend=FakeBackend()) for _ in range(2)]
        cluster = Cluster(apps)
        async with AsyncExitStack() as stack:
            for app in apps:
                await stack.enter_async_context(app.router.lifespan_context(app))
            async with cluster.http(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                async with Client(streamable_http_client(CLIENT.url, http_client=http)) as client:
                    for _ in range(4):
                        result = await client.call_tool("get_moderation_case", ARGS)
                        self.assertFalse(result.is_error)
            self.assertEqual(set(cluster.routed), {0, 1})
            self.assertTrue(all(value is None for value in cluster.session_ids))
            apps[0].state.draining = True
            cluster.routed.clear()
            read = BusinessMcpReadClient(CLIENT, http_client_factory=cluster.http)
            await read.call("get_moderation_case", ARGS)
            self.assertEqual(set(cluster.routed), {1})
            cluster.break_transport = True
            with self.assertRaises(AgentToolError) as raised:
                await read.call("get_moderation_case", ARGS)
            self.assertEqual(raised.exception.code, "TEMPORARY_UNAVAILABLE")
            self.assertNotIn("private", str(raised.exception))

    async def test_server_capacity_and_drain_release_slots(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Blocking(FakeBackend):
            async def get_case_context(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return await super().get_case_context(*args, **kwargs)

        app = create_app(replace(SETTINGS, max_concurrent_requests=1), backend=Blocking())
        cluster = Cluster([app])
        async with app.router.lifespan_context(app):
            read = BusinessMcpReadClient(CLIENT, http_client_factory=cluster.http)
            active = asyncio.create_task(read.call("get_moderation_case", ARGS))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                async with cluster.http(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                    busy = await http.post(CLIENT.url, json={})
                    self.assertEqual(busy.status_code, 503)
                    self.assertEqual(busy.headers["Retry-After"], "1")
                    app.state.draining = True
                    self.assertEqual((await http.get("http://localhost/ready")).status_code, 503)
                release.set()
                self.assertIn("data", await active)
            finally:
                release.set()
                await active
        self.assertFalse(app.state.started)
        self.assertTrue(app.state.draining)

    async def test_client_deadline_includes_shared_admission_and_caps_all_callers(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        class Blocking(FakeBackend):
            async def get_case_context(self, *args, **kwargs):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return await super().get_case_context(*args, **kwargs)

        app = create_app(SETTINGS, backend=Blocking())
        cluster = Cluster([app])
        settings = replace(CLIENT, max_concurrent_calls=1)
        async with app.router.lifespan_context(app):
            first = asyncio.create_task(BusinessMcpReadClient(
                settings, http_client_factory=cluster.http,
            ).call("get_moderation_case", ARGS))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                second = BusinessMcpReadClient(
                    replace(settings, timeout_seconds=0.05), http_client_factory=cluster.http,
                )
                with self.assertRaises(AgentToolError):
                    await second.call("get_moderation_case", ARGS)
                self.assertEqual(calls, 1)
            finally:
                release.set()
                await first
            recovered = BusinessMcpReadClient(settings, http_client_factory=cluster.http)
            self.assertIn("data", await recovered.call("get_moderation_case", ARGS))
            self.assertEqual(calls, 2)

    async def test_mutation_response_loss_is_unknown_then_status_is_observed_without_replay(self):
        posts = []
        applied = False

        def handler(request):
            nonlocal applied
            if request.method == "POST":
                posts.append(json.loads(request.content))
                applied = True
                raise httpx.ReadError("raw-chat-secret", request=request)
            return httpx.Response(200, json={"data": {
                "actionId": COMMAND["actionId"], "status": "APPLIED" if applied else "PENDING",
            }})

        async with httpx.AsyncClient(base_url=SETTINGS.backend_base_url,
                                     transport=httpx.MockTransport(handler)) as be_http:
            api = BackendApi(SETTINGS, be_http)
            apps = [create_app(SETTINGS, backend=api) for _ in range(2)]
            cluster = Cluster(apps)
            gateway = HttpModerationGateway(be_http, service_token="test", business_settings=CLIENT)
            async with AsyncExitStack() as stack:
                for app in apps:
                    await stack.enter_async_context(app.router.lifespan_context(app))
                with patch("app.clients.business_mcp.create_mcp_http_client", side_effect=cluster.http):
                    result = await gateway.kick(COMMAND, trace_id=str(TRACE_ID))
                    self.assertEqual(result, {"actionId": COMMAND["actionId"], "status": "UNKNOWN"})
                    observed = await gateway.read_action(action_id=COMMAND["actionId"], trace_id=str(TRACE_ID))
                self.assertEqual(observed["status"], "APPLIED")
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0]["idempotencyKey"], COMMAND["idempotencyKey"])

    async def test_mutation_transport_failure_never_becomes_retryable(self):
        cluster = Cluster([])
        cluster.break_transport = True
        with self.assertRaises(AgentToolError) as raised:
            await call_business_tool(CLIENT, "kick_user", {
                "actionCommand": COMMAND, "traceId": str(TRACE_ID),
            }, mutation=True, http_client_factory=cluster.http)
        self.assertEqual(raised.exception.code, "ACTION_OUTCOME_UNKNOWN")
        self.assertFalse(raised.exception.retryable)

    async def test_mcp_response_loss_after_success_does_not_repeat_applied_action(self):
        class Applied(FakeBackend):
            def __init__(self):
                self.commands = []

            async def execute_action(self, command, *, trace_id):
                self.commands.append(command)
                return await super().execute_action(command, trace_id=trace_id)

        be = Applied()
        app = create_app(SETTINGS, backend=be)
        lost = False

        async def lose_first_applied_response(scope, receive, send):
            async def lossy_send(message):
                nonlocal lost
                if be.commands and not lost and message["type"] == "http.response.start":
                    lost = True
                    raise httpx2.ReadError("private-response-detail")
                await send(message)
            await app(scope, receive, lossy_send)

        def http_factory(*, headers):
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(
                app=lose_first_applied_response,
            ), headers=headers)

        async with app.router.lifespan_context(app):
            with patch("app.clients.business_mcp.create_mcp_http_client", side_effect=http_factory):
                gateway = HttpModerationGateway(None, service_token="test", business_settings=CLIENT)
                result = await gateway.kick(COMMAND, trace_id=str(TRACE_ID))
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertTrue(lost)
                observed = await gateway.read_action(action_id=COMMAND["actionId"], trace_id=str(TRACE_ID))
                self.assertEqual(observed["status"], "APPLIED")
                self.assertEqual(len(be.commands), 1)
                self.assertEqual(str(be.commands[0].action_id), COMMAND["actionId"])
                self.assertEqual(be.commands[0].idempotency_key, COMMAND["idempotencyKey"])

    async def test_backend_mutation_bad_responses_and_network_errors_are_uncertain(self):
        failures = [
            httpx.ReadTimeout("secret"), httpx.WriteError("secret"),
            httpx.RemoteProtocolError("secret"), httpx.ConnectError("secret"),
            httpx.Response(200, text="raw-chat-secret"),
            httpx.Response(200, json=[]), httpx.Response(200, json={"data": []}),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                def handler(request):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                async with httpx.AsyncClient(base_url=SETTINGS.backend_base_url,
                                             transport=httpx.MockTransport(handler)) as http:
                    with self.assertRaises(BackendError) as raised:
                        await BackendApi(SETTINGS, http).execute_action(
                            KickCommand.model_validate(COMMAND), trace_id=str(TRACE_ID),
                        )
                self.assertEqual(raised.exception.code, "ACTION_OUTCOME_UNKNOWN")
                self.assertFalse(raised.exception.retryable)

    async def test_logs_health_and_repr_omit_secrets_and_invalid_tool_inputs(self):
        app = create_app(SETTINGS, backend=FakeBackend())
        cluster = Cluster([app])
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        try:
            async with app.router.lifespan_context(app):
                with self.assertRaises(AgentToolError):
                    await BusinessMcpReadClient(CLIENT, http_client_factory=cluster.http).call(
                        "get_chat_context", {**ARGS, "before": "raw-chat-secret"},
                    )
                async with cluster.http(headers={}) as http:
                    health = (await http.get("http://localhost/health")).text
            text = output.getvalue() + health + repr(SETTINGS) + repr(CLIENT)
            for secret in (TOKEN, SETTINGS.backend_service_token, "raw-chat-secret"):
                self.assertNotIn(secret, text)
        finally:
            logging.getLogger().removeHandler(handler)

    async def test_default_http_pool_is_explicitly_bounded(self):
        async with create_mcp_http_client(headers={}) as http:
            self.assertEqual(http.timeout.connect, 3)
            self.assertEqual(http.timeout.pool, 1)
            # httpx2 has no public pool statistics API.
            self.assertEqual(http._transport._pool._max_connections, 2)
