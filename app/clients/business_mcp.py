"""Agent-owned read boundary for the Business MCP server (locked SDK 2.3)."""

import asyncio
from dataclasses import dataclass, field
import json
import os
import re
import math
import weakref
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import httpx2
from app.clients.mcp_logging import protect_mcp_logs


def create_mcp_http_client(*, headers: dict[str, str]) -> httpx2.AsyncClient:
    # A session owns its pool; the shared per-loop semaphore bounds all sessions.
    return httpx2.AsyncClient(
        headers=headers, trust_env=False,
        limits=httpx2.Limits(max_connections=2, max_keepalive_connections=2),
        timeout=httpx2.Timeout(10, connect=3, pool=1),
    )


_call_limits: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, tuple[int, weakref.ReferenceType[asyncio.Semaphore]]],
] = weakref.WeakKeyDictionary()


def _call_semaphore(settings: "BusinessMcpSettings") -> asyncio.Semaphore:
    limits = _call_limits.setdefault(asyncio.get_running_loop(), {})
    key = settings.url
    existing = limits.get(key)
    semaphore = existing[1]() if existing else None
    if semaphore is None:
        semaphore = asyncio.Semaphore(settings.max_concurrent_calls)
        limits[key] = (settings.max_concurrent_calls, weakref.ref(semaphore))
    elif existing is not None and existing[0] != settings.max_concurrent_calls:
        raise ValueError("Inconsistent MCP concurrency for one endpoint")
    return semaphore


READ_TOOL_NAMES = frozenset({
    "get_moderation_case", "get_chat_context", "get_user_history",
    "get_stream_info", "get_official_links", "get_security_facts",
    "get_action_status",
})


class AgentToolError(Exception):
    """Safe error metadata; never embeds a response body, token or chat text."""

    def __init__(self, code: str, *, trace_id: str, retryable: bool = False,
                 retry_after: float = 0, usage: dict[str, int] | None = None):
        super().__init__(code)
        self.code = code
        self.trace_id = trace_id
        self.retryable = retryable
        self.retry_after = retry_after
        self.usage = usage


@dataclass(frozen=True)
class BusinessMcpSettings:
    url: str
    token: str = field(repr=False)
    timeout_seconds: float = 10.0
    max_concurrent_calls: int = 8

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("BUSINESS_MCP_URL must be an HTTP URL without credentials/query")
        if not self.token.strip():
            raise ValueError("BUSINESS_MCP_AGENT_TOKEN is required")
        if (not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
                or self.max_concurrent_calls < 1):
            raise ValueError("MCP timeout must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            url=os.getenv("BUSINESS_MCP_URL", "http://127.0.0.1:8002/mcp"),
            token=os.getenv("BUSINESS_MCP_AGENT_TOKEN", ""),
            timeout_seconds=float(os.getenv("BUSINESS_MCP_CALL_TIMEOUT", "10")),
            max_concurrent_calls=int(os.getenv("BUSINESS_MCP_CLIENT_CONCURRENCY", "8")),
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

    def __init__(self, settings: BusinessMcpSettings, *, http_client_factory=None):
        self._settings = settings
        self._http_client_factory = http_client_factory

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            trace_id = str(UUID(str(arguments.get("traceId", ""))))
        except ValueError:
            raise AgentToolError("INVALID_INPUT", trace_id="") from None
        if name not in READ_TOOL_NAMES:
            raise AgentToolError("FORBIDDEN", trace_id=trace_id)
        return await call_business_tool(self._settings, name, arguments,
                                        http_client_factory=self._http_client_factory)


async def call_business_tool(
    settings: BusinessMcpSettings, name: str, arguments: dict[str, Any], *,
    mutation: bool = False, http_client_factory=None,
) -> dict[str, Any]:
    """One fresh protocol session; no automatic transport/mutation retries."""
    trace_id = str(UUID(str(arguments["traceId"])))
    auth_error_code = None

    async def record_auth_status(response):
        # SDK 2.3 wraps HTTP authentication errors as generic protocol errors.
        nonlocal auth_error_code
        if response.status_code in {401, 403}:
            auth_error_code = "UNAUTHENTICATED" if response.status_code == 401 else "FORBIDDEN"

    semaphore = _call_semaphore(settings)
    protect_mcp_logs()
    try:
        async with asyncio.timeout(settings.timeout_seconds):
            async with semaphore:
                async with (http_client_factory or create_mcp_http_client)(
                    headers={"Authorization": f"Bearer {settings.token}"},
                ) as http_client:
                    http_client.event_hooks["response"].append(record_auth_status)
                    transport = streamable_http_client(settings.url, http_client=http_client)
                    async with Client(transport, read_timeout_seconds=settings.timeout_seconds) as client:
                        # Prime schema validation before effects. The high-level SDK call
                        # can re-list/replay on HEADER_MISMATCH; mutations must never do so.
                        await client.list_tools()
                        result = await client.session.call_tool(name, arguments)
                        return _decode_result(result, trace_id)
    except AgentToolError as error:
        if mutation and (error.retryable or error.code in {"INVALID_TOOL_RESPONSE", "TOOL_ERROR"}):
            raise AgentToolError("ACTION_OUTCOME_UNKNOWN", trace_id=trace_id) from None
        raise
    except TimeoutError:
        raise AgentToolError("ACTION_OUTCOME_UNKNOWN" if mutation else "TEMPORARY_UNAVAILABLE",
                             trace_id=trace_id, retryable=not mutation) from None
    except Exception as error:
        if auth_error_code:
            raise AgentToolError(auth_error_code, trace_id=trace_id) from None
        if mutation:
            raise AgentToolError("ACTION_OUTCOME_UNKNOWN", trace_id=trace_id) from None
        raise _transport_error(error, trace_id) from None
