import json
import unittest
from typing import Any
from uuid import UUID

import httpx
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server import MCPServer

from app.business_mcp.backend import BackendApi
from app.business_mcp.config import Settings
from app.business_mcp.schemas import ActionCommand, BackendError
from app.business_mcp.main import create_app
from app.business_mcp.tools import register_tools


CASE_ID = UUID("00000000-0000-4000-8000-000000000001")
TRACE_ID = UUID("00000000-0000-4000-8000-000000000002")


class FakeBackend:
    async def get_case_context(
        self,
        case_id: UUID,
        *,
        view: str,
        trace_id: str,
        params: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        return {
            "caseId": str(case_id),
            "view": view,
            "traceId": trace_id,
            "params": params or {},
        }

    async def get_user_history(
        self, case_id: UUID, *, days: int, trace_id: str
    ) -> dict[str, Any]:
        return {"caseId": str(case_id), "days": days}

    async def get_action_status(
        self, action_id: UUID, *, trace_id: str
    ) -> dict[str, Any]:
        return {"actionId": str(action_id), "status": "APPLIED"}

    async def execute_action(
        self, command: ActionCommand, *, trace_id: str
    ) -> dict[str, Any]:
        return {"actionId": str(command.action_id), "status": "APPLIED"}

    async def reconcile_review(
        self, case_id: UUID, decision_id: UUID, *, trace_id: str
    ) -> dict[str, Any]:
        return {"caseId": str(case_id), "decisionId": str(decision_id)}


class FailingBackend(FakeBackend):
    async def get_case_context(
        self,
        case_id: UUID,
        *,
        view: str,
        trace_id: str,
        params: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        raise BackendError(
            "STALE_EVIDENCE",
            "Evidence version changed",
            retryable=False,
            trace_id=trace_id,
        )


class BusinessMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_publish_camel_case_schema_and_trace_envelope(self) -> None:
        server = MCPServer("test")
        register_tools(server, FakeBackend)

        async with Client(server) as client:
            tools = await client.list_tools()
            tool = next(t for t in tools.tools if t.name == "get_moderation_case")
            self.assertIn("caseId", tool.input_schema["properties"])
            hide_tool = next(t for t in tools.tools if t.name == "hide_message")
            command_schema = hide_tool.input_schema["properties"]["actionCommand"]
            if "$ref" in command_schema:
                definition = command_schema["$ref"].rsplit("/", maxsplit=1)[-1]
                command_schema = hide_tool.input_schema["$defs"][definition]
            type_schema = command_schema["properties"]["type"]
            self.assertEqual(
                type_schema.get("const", type_schema.get("enum")),
                "HIDE" if "const" in type_schema else ["HIDE"],
            )

            result = await client.call_tool(
                "get_moderation_case",
                {"caseId": str(CASE_ID), "traceId": str(TRACE_ID)},
            )

        self.assertFalse(result.is_error)
        self.assertEqual(
            result.structured_content,
            {
                "data": {
                    "caseId": str(CASE_ID),
                    "view": "moderationCase",
                    "traceId": str(TRACE_ID),
                    "params": {},
                },
                "traceId": str(TRACE_ID),
            },
        )

    async def test_backend_errors_are_reported_as_mcp_tool_errors(self) -> None:
        server = MCPServer("test")
        register_tools(server, FailingBackend)

        async with Client(server) as client:
            result = await client.call_tool(
                "get_moderation_case",
                {"caseId": str(CASE_ID), "traceId": str(TRACE_ID)},
            )

        self.assertTrue(result.is_error)
        self.assertIn("STALE_EVIDENCE", result.content[0].text)
        self.assertIn(str(TRACE_ID), result.content[0].text)

    async def test_backend_adapter_sends_auth_trace_and_typed_query(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": {"count": 2}})

        settings = Settings(
            backend_base_url="http://backend.test",
            backend_service_token="service-token",
            agent_token="agent-token",
            host="127.0.0.1",
            port=8002,
        )
        async with httpx.AsyncClient(
            base_url=settings.backend_base_url,
            transport=httpx.MockTransport(handler),
        ) as client:
            backend = BackendApi(settings, client)
            result = await backend.get_case_context(
                CASE_ID,
                view="chatContext",
                trace_id=str(TRACE_ID),
                params={"before": 4, "after": 2},
            )

        self.assertEqual(result, {"count": 2})
        self.assertEqual(requests[0].url.path, f"/internal/cases/{CASE_ID}/context")
        self.assertEqual(
            dict(requests[0].url.params),
            {"view": "chatContext", "before": "4", "after": "2"},
        )
        self.assertEqual(
            requests[0].headers["authorization"], "Bearer service-token"
        )
        self.assertEqual(requests[0].headers["x-trace-id"], str(TRACE_ID))

    async def test_missing_backend_credential_is_an_explicit_tool_error(self) -> None:
        settings = Settings(
            backend_base_url="http://backend.test",
            backend_service_token="",
            agent_token="agent-token",
            host="127.0.0.1",
            port=8002,
        )
        async with httpx.AsyncClient(base_url=settings.backend_base_url) as client:
            backend = BackendApi(settings, client)
            with self.assertRaises(BackendError) as raised:
                await backend.get_action_status(UUID(int=3), trace_id=str(TRACE_ID))

        self.assertEqual(raised.exception.code, "UNAUTHENTICATED")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(
            json.loads(raised.exception.as_tool_error())["traceId"], str(TRACE_ID)
        )

    def test_http_endpoint_requires_agent_token(self) -> None:
        settings = Settings(
            backend_base_url="http://backend.test",
            backend_service_token="service-token",
            agent_token="agent-token",
            host="127.0.0.1",
            port=8002,
        )
        app = create_app(settings, backend=FakeBackend())

        with TestClient(app) as client:
            self.assertTrue(client.get("/health").json()["readyForTasks"])
            response = client.post("/mcp", json={})

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
