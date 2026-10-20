"""Agent-owned read boundary for the Business MCP server (locked SDK 2.3)."""

import asyncio
from dataclasses import dataclass, field
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


READ_TOOL_NAMES = frozenset({
    "get_moderation_case", "get_chat_context", "get_user_history",
    "get_stream_info", "get_official_links", "get_security_facts",
    "get_action_status",
})


class AgentToolError(Exception):
    """Safe error metadata; never embeds a response body, token or chat text."""

    def __init__(self, code: str, *, trace_id: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.trace_id = trace_id
        self.retryable = retryable


@dataclass(frozen=True)
class BusinessMcpSettings:
    url: str
    token: str = field(repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("BUSINESS_MCP_URL must be an HTTP URL without credentials/query")
        if not self.token.strip():
            raise ValueError("BUSINESS_MCP_AGENT_TOKEN is required")
        if self.timeout_seconds <= 0:
            raise ValueError("MCP timeout must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            url=os.getenv("BUSINESS_MCP_URL", "http://127.0.0.1:8002/mcp"),
            token=os.getenv("BUSINESS_MCP_AGENT_TOKEN", ""),
        )


def _leaves(error: Exception):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, ExceptionGroup):
            pending.extend(current.exceptions)
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)


def _transport_error(error: Exception, trace_id: str) -> AgentToolError:
    for leaf in _leaves(error):
        if isinstance(leaf, AgentToolError):
            return leaf
        # The SDK uses httpx2; do not confuse it with the BE adapter's httpx 0.x.
        status = getattr(getattr(leaf, "response", None), "status_code", None)
        if status in {401, 403}:
            return AgentToolError(
                "UNAUTHENTICATED" if status == 401 else "FORBIDDEN", trace_id=trace_id
            )
    return AgentToolError("TEMPORARY_UNAVAILABLE", trace_id=trace_id, retryable=True)


def _decode_result(result, trace_id: str) -> dict[str, Any]:
    if result.is_error:
        code, retryable = "TOOL_ERROR", False
        for content in result.content:
            if getattr(content, "type", None) != "text":
                continue
            try:
                # SDK 2.3 wraps ToolError text with this label on the wire.
                text = re.sub(r"^Error executing tool [a-z_]+: ", "", content.text, count=1)
                payload = json.loads(text)
            except (ValueError, TypeError):
                continue
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                candidate = error.get("code")
                if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", candidate):
                    code = candidate
                retryable = error.get("retryable") is True
                break
        raise AgentToolError(code, trace_id=trace_id, retryable=retryable)
    payload = result.structured_content
    if (not isinstance(payload, dict) or not isinstance(payload.get("data"), dict)
            or payload.get("traceId") != trace_id):
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
    return payload


class BusinessMcpReadClient:
    """Every call owns/closes its session. No mutations and no automatic retries."""

    def __init__(self, settings: BusinessMcpSettings):
        self._settings = settings

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            trace_id = str(UUID(str(arguments.get("traceId", ""))))
        except ValueError:
            raise AgentToolError("INVALID_INPUT", trace_id="") from None
        if name not in READ_TOOL_NAMES:
            raise AgentToolError("FORBIDDEN", trace_id=trace_id)
        auth_error_code = None

        async def record_auth_status(response):
            # SDK 2.3 turns non-JSON-RPC HTTP errors into a generic MCP error.
            # Record only status so authentication remains explicit without logging bodies.
            nonlocal auth_error_code
            if response.status_code in {401, 403}:
                auth_error_code = "UNAUTHENTICATED" if response.status_code == 401 else "FORBIDDEN"

        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                async with create_mcp_http_client(
                    headers={"Authorization": f"Bearer {self._settings.token}"},
                ) as http_client:
                    http_client.event_hooks["response"].append(record_auth_status)
                    transport = streamable_http_client(self._settings.url, http_client=http_client)
                    async with Client(transport, read_timeout_seconds=self._settings.timeout_seconds) as client:
                        result = await client.call_tool(name, arguments)
                        return _decode_result(result, trace_id)
        except AgentToolError:
            raise
        except TimeoutError:
            raise AgentToolError("TEMPORARY_UNAVAILABLE", trace_id=trace_id, retryable=True) from None
        except Exception as error:
            if auth_error_code:
                raise AgentToolError(auth_error_code, trace_id=trace_id) from None
            raise _transport_error(error, trace_id) from None
