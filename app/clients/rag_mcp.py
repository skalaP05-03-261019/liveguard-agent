"""Agent-owned consumer of AI2 issue #4; no indexing, mutations or retries."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import math
import os
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.exceptions import MCPError
from pydantic import ConfigDict, Field, ValidationError

from app.clients.business_mcp import AgentToolError, _decode_result, _leaves, _transport_error
from app.schemas import Category, Contract


RAG_TOOL_NAMES = frozenset({"search_policy", "get_policy_section"})
MAX_POLICY_TEXT_CHARS = 24_000  # Local consumer cap; never silently truncate evidence.


@dataclass(frozen=True)
class RagMcpSettings:
    url: str
    token: str = field(repr=False)
    timeout_seconds: float = 10.0

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("RAG_MCP_URL must be an HTTP URL without credentials/query")
        if not self.token.strip() or any(c in self.token for c in "\r\n"):
            raise ValueError("RAG_MCP_AGENT_TOKEN is required and must be a single line")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("RAG timeout must be finite and positive")

    @classmethod
    def from_env(cls):
        # Do not reuse a Business MCP credential or guess a RAG address/version.
        return cls(url=os.getenv("RAG_MCP_URL", ""),
                   token=os.getenv("RAG_MCP_AGENT_TOKEN", ""))


class SearchArguments(Contract):
    query: str = Field(min_length=1, max_length=1000, strict=True)
    category: Category | None = None
    policy_version: str = Field(min_length=1, strict=True)
    top_k: int = Field(default=4, ge=1, le=4, strict=True)
    trace_id: UUID


class SectionArguments(Contract):
    policy_id: str = Field(min_length=1, strict=True)
    policy_version: str = Field(min_length=1, strict=True)
    trace_id: UUID


class PolicySection(Contract):
    # Providers may add metadata; consumed fields are strictly typed.
    model_config = ConfigDict(**{**Contract.model_config, "extra": "ignore"})
    policy_id: str = Field(min_length=1, strict=True)
    title: str = Field(min_length=1, strict=True)
    text: str = Field(min_length=1, max_length=MAX_POLICY_TEXT_CHARS, strict=True)
    version: str = Field(min_length=1, strict=True)
    resource_uri: str = Field(min_length=1, strict=True)


class PolicyMatch(PolicySection):
    score: float = Field(allow_inf_nan=False, strict=True)


class SearchData(Contract):
    matches: list[PolicyMatch] = Field(max_length=4)


def require_trace(trace_id: str) -> str:
    try:
        return str(UUID(str(trace_id)))
    except (ValueError, TypeError, AttributeError):
        raise AgentToolError("INVALID_INPUT", trace_id="") from None


def validate_policy_uri(uri: str, version: str, trace_id: str) -> None:
    """Validate versioned identity without interpreting the URI as a network URL."""
    try:
        parts = urlsplit(uri)
        path = parts.path.split("/")
        segments = [unquote(s) for s in [parts.netloc, *path[1:]]]
        valid = (parts.scheme == "policy" and not parts.query and not parts.fragment
                 and not parts.username and not parts.password and len(segments) == 3
                 and all(s and s not in {".", ".."}
                         and not any(c.isspace() or c in "/\\?#%" for c in s)
                         for s in segments))
    except ValueError:
        valid = False
    if not valid:
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
    if segments[0] != version:
        raise AgentToolError("POLICY_MISMATCH", trace_id=trace_id)


def parse_section(data: Any, version: str, trace_id: str,
                  *, policy_id: str | None = None) -> PolicySection:
    try:
        section = PolicySection.model_validate(data)
    except ValidationError:
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id) from None
    if section.version != version:
        raise AgentToolError("POLICY_MISMATCH", trace_id=trace_id)
    if not all(value.strip() for value in (section.policy_id, section.title, section.text)):
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
    validate_policy_uri(section.resource_uri, version, trace_id)
    if policy_id is not None and section.policy_id != policy_id:
        raise AgentToolError("INVALID_POLICY_EVIDENCE", trace_id=trace_id)
    return section


def parse_matches(data: Any, version: str, top_k: int, trace_id: str) -> list[PolicyMatch]:
    try:
        matches = SearchData.model_validate(data).matches
    except ValidationError:
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id) from None
    if len(matches) > top_k:
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
    ids, uris = set(), set()
    for match in matches:
        parse_section(match.model_dump(by_alias=True), version, trace_id)
        if match.policy_id in ids or match.resource_uri in uris:
            raise AgentToolError("INVALID_POLICY_EVIDENCE", trace_id=trace_id)
        ids.add(match.policy_id)
        uris.add(match.resource_uri)
    return matches


class RagMcpReadClient:
    """Only AI2 search/section tools and policy:// Resources; one attempt per call."""

    def __init__(self, settings: RagMcpSettings):
        self._settings = settings

    @asynccontextmanager
    async def _session(self, trace_id: str):
        auth_error = None

        async def record_status(response):
            nonlocal auth_error
            if response.status_code in {401, 403}:
                auth_error = "UNAUTHENTICATED" if response.status_code == 401 else "FORBIDDEN"

        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                async with create_mcp_http_client(
                    headers={"Authorization": f"Bearer {self._settings.token}"},
                ) as http_client:
                    http_client.event_hooks["response"].append(record_status)
                    transport = streamable_http_client(self._settings.url, http_client=http_client)
                    async with Client(
                        transport, read_timeout_seconds=self._settings.timeout_seconds,
                        input_required_max_rounds=0, cache=None,
                    ) as client:
                        yield client
        except AgentToolError:
            raise
        except TimeoutError:
            raise AgentToolError("TEMPORARY_UNAVAILABLE", trace_id=trace_id, retryable=True) from None
        except Exception as error:
            if auth_error:
                raise AgentToolError(auth_error, trace_id=trace_id) from None
            if any(isinstance(leaf, MCPError) for leaf in _leaves(error)):
                # Resource/protocol errors are not blindly retried as transport failures.
                raise AgentToolError("MCP_REQUEST_FAILED", trace_id=trace_id) from None
            raise _transport_error(error, trace_id) from None

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        trace_id = require_trace(arguments.get("traceId", ""))
        if name not in RAG_TOOL_NAMES:
            raise AgentToolError("FORBIDDEN", trace_id=trace_id)
        try:
            args = (SearchArguments if name == "search_policy" else SectionArguments).model_validate(arguments)
        except ValidationError:
            raise AgentToolError("INVALID_INPUT", trace_id=trace_id) from None
        if not args.policy_version.strip() or (
            isinstance(args, SearchArguments) and not args.query.strip()
        ):
            raise AgentToolError("INVALID_INPUT", trace_id=trace_id)
        async with self._session(trace_id) as client:
            result = await client.call_tool(name, args.model_dump(mode="json", by_alias=True, exclude_none=True))
            payload = _decode_result(result, trace_id)
            if isinstance(args, SearchArguments):
                parse_matches(payload["data"], args.policy_version, args.top_k, trace_id)
            else:
                parse_section(payload["data"], args.policy_version, trace_id, policy_id=args.policy_id)
            return payload

    async def read_resource(self, uri: str, *, policy_version: str,
                            trace_id: str) -> dict[str, Any]:
        trace_id = require_trace(trace_id)
        validate_policy_uri(uri, policy_version, trace_id)
        async with self._session(trace_id) as client:
            result = await client.read_resource(uri)
            # AI2 resource representation is not frozen: support one text/plain or
            # text/markdown body. Other encodings fail explicitly for contract review.
            if len(result.contents) != 1:
                raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
            content = result.contents[0]
            text = getattr(content, "text", None)
            if (str(content.uri) != uri or content.mime_type not in {None, "text/plain", "text/markdown"}
                    or not isinstance(text, str) or not text
                    or len(text) > MAX_POLICY_TEXT_CHARS):
                raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
            # This trace ID is local correlation. resources/read has no tool-envelope echo.
            return {"data": {"resourceUri": uri, "text": text}, "traceId": trace_id}
