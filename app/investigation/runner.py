"""Model-selected, read-only investigation. No top-level Graph or approval writes.

ExecutionGate and present_data are seams for AI2 #6/#7. The standalone default
fails closed with finite calls/time and basic masking, and has no retry policy.
"""

import asyncio
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable, Protocol

from pydantic import Field, ValidationError

from app.clients.business_mcp import AgentToolError
from app.clients.rag_mcp import require_trace
from app.policy_evidence import retrieve_policy_evidence
from app.schemas import Category, Contract, MessageFinding
from app.investigation.model import InvestigationModel, ModelTurn
from app.investigation.validation import validate_analysis


BUSINESS_TOOLS = frozenset({"get_chat_context", "get_user_history", "get_stream_info",
                            "get_official_links", "get_security_facts"})
PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "investigator.md"


class SearchRequest(Contract):
    query: str = Field(min_length=1, max_length=1000, strict=True)
    category: Category | None = None
    top_k: int = Field(default=2, ge=1, le=3, strict=True)


class FinishRequest(Contract):
    message_findings: list[MessageFinding] = Field(min_length=1, max_length=100)


def _function(name, description, schema):
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": schema, "strict": False,
    }}


def tool_definitions():
    definitions = [_function("search_policy", "현재 사건과 같은 버전의 정책과 정상 예외를 검색하고 원문을 검증합니다.",
                             SearchRequest.model_json_schema(by_alias=True)),
                   _function("finish_investigation", "각 TARGET의 최종 판정을 제출. 제재 기간·명령 금지.",
                             FinishRequest.model_json_schema(by_alias=True))]
    descriptions = {
        "get_chat_context": "현재 사건의 주변 대화. 다른 사건 조회 불가.",
        "get_user_history": "현재 사건 사용자의 유형별 확정 이력. 조회 기간은 서버 정책이며 강퇴 집계로 사용 금지.",
        "get_stream_info": "현재 방송의 공식 이벤트와 방송 맥락 조회.",
        "get_official_links": "현재 방송에 등록된 공식 링크 조회. 외부 링크 방문 안 함.",
        "get_security_facts": "개인정보/공식 연락처 여부의 서버 검증 사실 및 UNKNOWN 조회.",
    }
    for name in sorted(BUSINESS_TOOLS):
        definitions.append(_function(name, descriptions[name],
                                    {"type": "object", "properties": {}, "additionalProperties": False}))
    return definitions


class ExecutionGate(Protocol):
    async def run(self, name: str, operation: Callable[[], Awaitable[Any]]) -> Any: ...


class StandaloneGate:
    """Replace with the shared #6 gate at integration; never wrap it with another budget.

    Recommended 8 model calls / 3 same tool / 120 active seconds. No SDK or local
    retries. Existing turn_count and tool_observations consume this run's budget.
    This function never waits for approval; durable clock accounting belongs to #6.
    """

    def __init__(self, state, trace_id):
        self.trace_id = trace_id
        self.models = state.get("turn_count", 0)
        self.calls = Counter(o["toolName"] for o in state.get("tool_observations", []) if "toolName" in o)
        self.deadline = time.monotonic() + 120

    async def run(self, name, operation):
        if name == "model":
            self.models += 1
            exceeded = self.models > 8
        else:
            self.calls[name] += 1
            exceeded = self.calls[name] > 3
        remaining = self.deadline - time.monotonic()
        if exceeded or remaining <= 0:
            raise AgentToolError("INVESTIGATION_LIMIT", trace_id=self.trace_id)
        try:
            async with asyncio.timeout(remaining):
                return await operation()
        except TimeoutError:
            raise AgentToolError("INVESTIGATION_LIMIT", trace_id=self.trace_id) from None


def basic_presentation(value):
    """Standalone transport hygiene, not a substitute for AI2 #7 security evaluation."""
    if isinstance(value, dict):
        return {k: basic_presentation(v) for k, v in value.items()}
    if isinstance(value, list):
        return [basic_presentation(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
        value = re.sub(r"(?<!\d)\d{2,4}[- .]\d{3,4}[- .]\d{4}(?!\d)|(?<!\d)01[016789]\d{7,8}(?!\d)", "[PHONE]", value)
        value = re.sub(r"(?<!\d)\d{10,16}(?!\d)", "[ACCOUNT_OR_ID]", value)
        return value[:12_000] + ("[TRUNCATED]" if len(value) > 12_000 else "")
    return value


class _GuardedPolicy:
    def __init__(self, client, gate, observations, trace_id):
        self.client, self.gate, self.observations, self.trace_id = client, gate, observations, trace_id

    async def call(self, name, arguments):
        self.observations.append({"toolName": name, "traceId": self.trace_id})
        return await self.gate.run(name, lambda: self.client.call(name, arguments))

    async def read_resource(self, uri, **kwargs):
        self.observations.append({"toolName": "read_policy_resource", "traceId": self.trace_id})
        return await self.gate.run("read_policy_resource", lambda: self.client.read_resource(uri, **kwargs))


async def run_investigation(state: dict, *, model: InvestigationModel, business, policy,
                            gate: ExecutionGate | None = None,
                            present_data: Callable[[Any], Any] = basic_presentation) -> dict:
    """Return an atomic patch; errors do not publish model findings or mutate input.

    Backend IDs, version and trace are bound by runtime, never accepted from model.
    Caller must load a validated case snapshot first. Model-visible data is separate
    from raw evidence. #6 must pass one shared gate across all nodes/retries.
    """
    working = deepcopy(state)
    trace_id = require_trace(working.get("trace_id", ""))
    try:
        if not working["target_messages"] or not working["policy_version"]:
            raise ValueError
        # Reject malformed snapshot identity before sending data to any model.
        from uuid import UUID
        UUID(working["case_id"])
        UUID(working["snapshot_id"])
        if not isinstance(working["evidence_version"], int) or working["evidence_version"] < 1:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise AgentToolError("INVALID_INPUT", trace_id=trace_id) from None
    active_gate = gate if gate is not None else StandaloneGate(working, trace_id)
    observations = list(working.get("tool_observations", []))
    policies = _GuardedPolicy(policy, active_gate, observations, trace_id)
    initial = json.dumps(present_data({
                    "dataKind": "UNTRUSTED_CHAT_DATA", "targetMessages": working["target_messages"],
                    "contextMessages": working.get("context_messages", []),
                    "policyVersion": working["policy_version"],
                }), ensure_ascii=False)
    if len(initial) > 48_000:
        raise AgentToolError("MODEL_CONTEXT_LIMIT", trace_id=trace_id)
    incomplete_context = "[TRUNCATED]" in initial
    messages = [{"role": "system", "content": PROMPT.read_text()},
                {"role": "user", "content": initial}]
    usage = []
    seen_calls = set()
    turns = working.get("turn_count", 0)
    # The execution gate counts all attempts, including low-level MCP calls.
    # A defensive hard ceiling also protects against a misconfigured external gate.
    for _ in range(8):
        turn = await active_gate.run("model", lambda: model.complete(
            deepcopy(messages), tool_definitions(), trace_id=trace_id))
        turns += 1
        if not isinstance(turn, ModelTurn) or len(turn.calls) != 1:
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id)
        call = turn.calls[0]
        if (not isinstance(call.id, str) or not call.id or len(call.id) > 200
                or call.id in seen_calls or not isinstance(call.arguments, dict)
                or not isinstance(call.name, str)):
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id)
        seen_calls.add(call.id)
        usage.append({"toolName": "model", "traceId": trace_id, "turn": turns,
                      "usage": {k: v for k, v in turn.usage.items()
                                if k in {"inputTokens", "outputTokens"} and type(v) is int and v >= 0}})
        if call.name == "finish_investigation":
            try:
                findings = FinishRequest.model_validate(call.arguments)
            except ValidationError:
                raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id) from None
            payload = {"caseId": working["case_id"], "snapshotId": working["snapshot_id"],
                       "evidenceVersion": working["evidence_version"], "policyVersion": working["policy_version"],
                       "messageFindings": [f.model_dump(mode="json", by_alias=True) for f in findings.message_findings]}
            result = validate_analysis(working, payload)
            if incomplete_context and any(f.verdict != "NEEDS_REVIEW" for f in result.message_findings):
                raise AgentToolError("INCOMPLETE_MODEL_CONTEXT", trace_id=trace_id)
            # Dedicated state may contain evidence. Generic traces contain no text.
            return {"message_findings": result.message_findings,
                    "policy_evidence": working.get("policy_evidence", []),
                    "tool_observations": observations + usage, "turn_count": turns}
        # Validate allowlist/arguments before network. No prompt can grant extra tools.
        if call.name not in BUSINESS_TOOLS | {"search_policy"}:
            raise AgentToolError("FORBIDDEN", trace_id=trace_id)
        messages.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": call.id, "type": "function", "function": {
                "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}}]})
        try:
            if call.name == "search_policy":
                args = SearchRequest.model_validate(call.arguments)
                # Policy wrapper meters every actual search, section and Resource request.
                working.update(await retrieve_policy_evidence(
                    working, query=present_data(args.query), category=args.category,
                    top_k=args.top_k, tools=policies))
                body = {"policyEvidence": working["policy_evidence"]}
            else:
                if call.arguments:
                    raise ValueError("No model-provided business arguments allowed")
                arguments = {"caseId": working["case_id"], "traceId": trace_id}
                if call.name == "get_chat_context":
                    arguments.update(before=5, after=5)
                observations.append({"toolName": call.name, "traceId": trace_id})
                response = await active_gate.run(call.name, lambda: business.call(call.name, arguments))
                if (not isinstance(response, dict) or response.get("traceId") != trace_id
                        or not isinstance(response.get("data"), dict)):
                    raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
                # Additional chat context must match the original immutable snapshot;
                # new/unversioned messages never become admissible evidence.
                if call.name == "get_chat_context":
                    from app.case_loader import ChatContext
                    try:
                        context = ChatContext.model_validate(response["data"])
                    except ValidationError:
                        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id) from None
                    if (str(context.case_id) != working["case_id"]
                            or str(context.snapshot_id) != working["snapshot_id"]
                            or context.evidence_version != working["evidence_version"]):
                        raise AgentToolError("STALE_EVIDENCE", trace_id=trace_id)
                    if context.model_dump(mode="json", by_alias=True)["messages"] != working["messages"]:
                        raise AgentToolError("STALE_EVIDENCE", trace_id=trace_id)
                body = response["data"]
        except (ValidationError, ValueError):
            body = {"error": {"code": "INVALID_INPUT"}}
        except AgentToolError as error:
            if error.code != "NO_POLICY_EVIDENCE":
                # Dependency failures are terminal; never a model-generated success.
                raise
            body = {"error": {"code": error.code}}
        presented = present_data(body)
        content = json.dumps({"dataKind": "UNTRUSTED_TOOL_DATA", "data": presented}, ensure_ascii=False)
        incomplete_context = incomplete_context or "[TRUNCATED]" in content
        if len(content) > 48_000:
            # Never silently omit a policy and then claim its original was checked by the model.
            raise AgentToolError("MODEL_CONTEXT_LIMIT", trace_id=trace_id)
        messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    raise AgentToolError("INVESTIGATION_LIMIT", trace_id=trace_id)
