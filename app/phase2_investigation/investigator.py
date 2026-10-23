"""Phase 2 context investigation (#71) on the existing read-only loop and Harness budget.

Adds candidate hints and the Phase 2 prompt section to the case's policy-version investigator
prompt. The output contract, policy validation and execution limits are unchanged; no Backend writes.
"""

from pathlib import Path

from app.clients.business_mcp import AgentToolError
from app.harness.execution import HarnessGate, default_observer, run_guarded
from app.harness.safety import safe_presentation
from app.investigation.runner import prompt_for_policy, run_investigation
from app.phase2_bridge.contracts import InvestigationDraft, InvestigationHandoff, ScreenResult
from app.schemas import AnalysisResult

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
ADDENDUM = PROMPTS / "investigator-phase2.md"
VERSION_MARKER = "<!-- policy-version-section -->"
# v3.1 keeps CP-6.1 review for pure group hate; v3.2 investigates it under CP-3.3. Never backported.
VERSION_SECTIONS = {"v3.2": PROMPTS / "investigator-phase2-v3.2.md"}
DEFAULT_SECTION = PROMPTS / "investigator-phase2-v3.1.md"


def addendum_for(version: str) -> str:
    section = VERSION_SECTIONS.get(version, DEFAULT_SECTION).read_text(encoding="utf-8").rstrip()
    return ADDENDUM.read_text(encoding="utf-8").replace(VERSION_MARKER, section)


def system_prompt(version: str) -> str:
    # The case's policy-version rules stay first so shared fixes apply to both investigators.
    return prompt_for_policy(version).rstrip() + "\n\n" + addendum_for(version)


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
