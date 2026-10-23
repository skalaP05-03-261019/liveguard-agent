"""Phase 2 candidate-guided investigation on the existing read-only loop."""

from pathlib import Path

from app.clients.business_mcp import AgentToolError
from app.harness.execution import HarnessGate, default_observer, run_guarded
from app.harness.safety import safe_presentation
from app.investigation.runner import prompt_for_policy, run_investigation
from app.phase2_bridge.contracts import InvestigationDraft, InvestigationHandoff, ScreenResult
from app.schemas import AnalysisResult

ADDENDUM = Path(__file__).resolve().parents[2] / "prompts" / "investigator-phase2.md"
ADDENDUM_V32 = ADDENDUM.with_name("investigator-phase2-v3.2.md")


def system_prompt(version: str = "v3.1") -> str:
    """Compose one policy version with its matching candidate-hint instructions."""
    extra = ADDENDUM_V32 if version == "v3.2" else ADDENDUM
    return prompt_for_policy(version).rstrip() + "\n\n" + extra.read_text(encoding="utf-8")


def candidate_hints(screening: ScreenResult) -> dict:
    """Forward detector kinds and strengths, never detector reasoning as evidence."""
    assessment = screening.assessment
    return {"signals": [{"kind": s.kind.value, "strength": s.strength.value} for s in assessment.signals],
            "missingFacts": list(assessment.missing_facts)}


class Phase2Investigator:
    def __init__(self, *, model, business, policy, observer=None):
        self.model, self.business, self.policy, self.observer = model, business, policy, observer

    async def investigate(self, request: InvestigationHandoff) -> InvestigationDraft:
        state = request.agent_state()
        gate = HarnessGate.from_state(state, observer=self.observer or default_observer())
        patch = await run_guarded(state, lambda g: run_investigation(
            state, model=self.model, business=self.business, policy=self.policy, gate=g,
            present_data=safe_presentation, prompt=system_prompt(state["policy_version"]),
            extra_input={"candidateHints": candidate_hints(request.screening)}), gate=gate)
        if patch.get("task_outcome") or patch.get("error"):
            error = patch.get("error") or {}
            raise AgentToolError(error.get("code", "INVESTIGATION_FAILED"), trace_id=state["trace_id"])
        analysis = AnalysisResult(caseId=state["case_id"], snapshotId=state["snapshot_id"],
                                  evidenceVersion=state["evidence_version"], policyVersion=state["policy_version"],
                                  messageFindings=patch["message_findings"])
        return InvestigationDraft(analysis=analysis, policyEvidence=patch.get("policy_evidence", []),
                                  harnessBudget=patch.get("harness_budget", {}))
