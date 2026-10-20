"""Verify discovered policy evidence before exposing a State patch to investigation.

No model call, verdict, mutation or implicit retry. #9 chooses when to invoke this
boundary; #6/#7 wrap calls with the shared budget, masking and trace policy.
"""

from typing import Any, Protocol

from pydantic import ValidationError

from app.clients.business_mcp import AgentToolError
from app.clients.ports import ReadTools
from app.clients.rag_mcp import (
    RagMcpReadClient, RagMcpSettings, SearchArguments,
    parse_matches, parse_section, require_trace,
)
from app.state import AgentState


class PolicyTools(ReadTools, Protocol):
    async def read_resource(self, uri: str, *, policy_version: str,
                            trace_id: str) -> dict[str, Any]: ...


def _data(payload: Any, trace_id: str) -> dict[str, Any]:
    if (not isinstance(payload, dict) or payload.get("traceId") != trace_id
            or not isinstance(payload.get("data"), dict)):
        raise AgentToolError("INVALID_TOOL_RESPONSE", trace_id=trace_id)
    return payload["data"]


async def retrieve_policy_evidence(
    state: AgentState, *, query: str, category: str | None = None,
    top_k: int = 4, tools: PolicyTools | None = None,
) -> AgentState:
    """Return an atomic State patch only after all search→section→Resource checks.

    policyVersion comes from the loaded case, never a guessed default. Empty
    searches are NO_POLICY_EVIDENCE, not NO_VIOLATION. Full evidence is stored only
    in dedicated State; generic observations contain IDs, not raw policy/query.
    """
    trace_id = require_trace(state.get("trace_id", ""))
    try:
        args = SearchArguments.model_validate({
            "query": query, "category": category, "topK": top_k,
            "policyVersion": state.get("policy_version", ""), "traceId": trace_id,
        })
    except ValidationError:
        raise AgentToolError("INVALID_INPUT", trace_id=trace_id) from None
    if not args.query.strip() or not args.policy_version.strip():
        raise AgentToolError("INVALID_INPUT", trace_id=trace_id)
    client = tools if tools is not None else RagMcpReadClient(RagMcpSettings.from_env())
    response = await client.call("search_policy", args.model_dump(mode="json", by_alias=True, exclude_none=True))
    matches = parse_matches(_data(response, trace_id), args.policy_version, top_k, trace_id)
    if not matches:
        raise AgentToolError("NO_POLICY_EVIDENCE", trace_id=trace_id)

    evidence = {}
    for saved in state.get("policy_evidence", []):
        section = parse_section(saved, args.policy_version, trace_id)
        item = section.model_dump(mode="json", by_alias=True)
        if section.policy_id in evidence and evidence[section.policy_id] != item:
            raise AgentToolError("POLICY_MISMATCH", trace_id=trace_id)
        evidence[section.policy_id] = item
    observations = [*state.get("tool_observations", []),
                    {"toolName": "search_policy", "traceId": trace_id, "matchCount": len(matches)}]
    for match in matches:
        section_response = await client.call("get_policy_section", {
            "policyId": match.policy_id, "policyVersion": args.policy_version, "traceId": trace_id,
        })
        section = parse_section(_data(section_response, trace_id), args.policy_version,
                                trace_id, policy_id=match.policy_id)
        if section.resource_uri != match.resource_uri or match.text not in section.text:
            raise AgentToolError("INVALID_POLICY_EVIDENCE", trace_id=trace_id)
        resource = _data(await client.read_resource(
            section.resource_uri, policy_version=args.policy_version, trace_id=trace_id,
        ), trace_id)
        if resource.get("resourceUri") != section.resource_uri or resource.get("text") != section.text:
            raise AgentToolError("INVALID_POLICY_EVIDENCE", trace_id=trace_id)
        item = section.model_dump(mode="json", by_alias=True)
        prior = evidence.get(section.policy_id)
        if prior is not None and prior != item:
            raise AgentToolError("POLICY_MISMATCH", trace_id=trace_id)
        evidence[section.policy_id] = item
        observations.extend([
            {"toolName": "get_policy_section", "traceId": trace_id, "policyId": section.policy_id},
            {"resourceUri": section.resource_uri, "traceId": trace_id},
        ])
    return {"policy_evidence": list(evidence.values()), "tool_observations": observations}
