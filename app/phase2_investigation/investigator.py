"""Phase 2 context investigation (#71) on the existing read-only loop and Harness budget.

Adds candidate hints and the Phase 2 prompt section to the Phase 1 investigator. The
output contract, policy validation and execution limits are unchanged; no Backend writes.
"""

from pathlib import Path

from app.clients.business_mcp import AgentToolError
from app.harness.execution import HarnessGate, default_observer, run_guarded
from app.harness.safety import safe_presentation
from app.investigation.runner import PROMPT, run_investigation
from app.phase2_bridge.contracts import InvestigationDraft, InvestigationHandoff, ScreenResult
from app.schemas import AnalysisResult

ADDENDUM = Path(__file__).resolve().parents[2] / "prompts" / "investigator-phase2.md"


def system_prompt() -> str:
    # Phase 1 rules stay first so shared fixes (e.g. role semantics) apply to both investigators.
    return PROMPT.read_text().rstrip() + "\n\n" + ADDENDUM.read_text()


def candidate_hints(screening: ScreenResult) -> dict:
    """Signal kinds/strengths and detector missing facts only. Detector reasons are not forwarded."""
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
            present_data=safe_presentation, prompt=system_prompt(),
            extra_input={"candidateHints": candidate_hints(request.screening)}), gate=gate)
        if patch.get("task_outcome") or patch.get("error"):
            error = patch.get("error") or {}
            raise AgentToolError(error.get("code", "INVESTIGATION_FAILED"), trace_id=state["trace_id"])
        analysis = AnalysisResult(caseId=state["case_id"], snapshotId=state["snapshot_id"],
                                  evidenceVersion=state["evidence_version"], policyVersion=state["policy_version"],
                                  messageFindings=patch["message_findings"])
        return InvestigationDraft(analysis=analysis, policyEvidence=patch.get("policy_evidence", []),
                                  harnessBudget=patch.get("harness_budget", {}))
