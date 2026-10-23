"""Deterministic provenance validation, not proof that a model's meaning is correct."""

from pydantic import ValidationError

from app.clients.business_mcp import AgentToolError
from app.clients.rag_mcp import parse_section
from app.schemas import AnalysisResult, Category, Judgment


def validate_analysis(state: dict, payload: dict) -> AnalysisResult:
    trace_id = state.get("trace_id", "")
    try:
        result = AnalysisResult.model_validate(payload)
    except (ValidationError, TypeError):
        raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id) from None
    if (str(result.case_id) != state["case_id"]
            or str(result.snapshot_id) != state["snapshot_id"]
            or result.evidence_version != state["evidence_version"]):
        raise AgentToolError("STALE_EVIDENCE", trace_id=trace_id)
    if result.policy_version != state["policy_version"]:
        raise AgentToolError("POLICY_MISMATCH", trace_id=trace_id)
    targets = {m["messageId"] for m in state["target_messages"]}
    actual = {str(f.message_id) for f in result.message_findings}
    known = {m["messageId"] for m in state["messages"]}
    if not targets or actual != targets:
        raise AgentToolError("INVALID_EVIDENCE", trace_id=trace_id)
    policies = {}
    for data in state.get("policy_evidence", []):
        section = parse_section(data, result.policy_version, trace_id)
        policies[section.policy_id] = section
    for finding in result.message_findings:
        if (result.policy_version == "v3.2"
                and state.get("handling_route") == "PII_INVESTIGATION"
                and finding.verdict == Judgment.VIOLATION
                and finding.categories != [Category.PII_EXPOSURE]):
            # BE v3.2 confirms this route without a human only for PII alone.
            # Mixed-category output must be investigated as NEEDS_REVIEW, never
            # submitted to BE as an automatic confirmed violation (422).
            raise AgentToolError("INVALID_MODEL_OUTPUT", trace_id=trace_id)
        refs = [str(mid) for mid in finding.evidence_message_ids]
        if (len(refs) != len(set(refs)) or not set(refs) <= known
                or len(finding.policy_ids) != len(set(finding.policy_ids))
                or not set(finding.policy_ids) <= policies.keys()
                or not finding.reason.strip()):
            raise AgentToolError("INVALID_EVIDENCE", trace_id=trace_id)
        if finding.verdict != Judgment.NEEDS_REVIEW:
            # Normality also needs a policy/exception check, not only a model opinion.
            if not finding.policy_ids or str(finding.message_id) not in refs:
                raise AgentToolError("INVALID_EVIDENCE", trace_id=trace_id)
        if finding.verdict == Judgment.VIOLATION:
            covered = {policies[pid].category for pid in finding.policy_ids}
            if not set(finding.categories) <= covered:
                raise AgentToolError("INVALID_POLICY_EVIDENCE", trace_id=trace_id)
    return result
