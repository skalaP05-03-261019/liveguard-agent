"""Fetch immutable BE evidence and validate a trusted investigator callback, without submitting it."""

import asyncio
import math
from typing import Protocol
from pydantic import ValidationError
from app.case_loader import load_case_data
from app.clients.business_mcp import AgentToolError
from app.harness.execution import guarded_investigate
from app.investigation.validation import validate_analysis
from app.schemas import AnalysisResult, TaskEnvelope
from app.candidate_detection.contracts import validate_evidence
from .contracts import ScreenRequest, ScreenResult, InvestigationDraft, InvestigationHandoff


async def prepare_handoff(*, task: TaskEnvelope, request: ScreenRequest, result: ScreenResult, business):
    trace = str(task.trace_id)
    if (result.request_id != request.request_id or result.experiment != request.experiment
            or result.live_id != request.input.live_id
            or result.target_message_id != request.input.target.message_id):
        raise AgentToolError('SCREENING_BINDING_MISMATCH', trace_id=trace)
    if task.kind not in {'ANALYZE', 'REANALYZE'}:
        raise AgentToolError('INVALID_TASK_KIND', trace_id=trace)
    try:
        validate_evidence(request.input, result.assessment)
    except ValueError:
        raise AgentToolError('INVALID_SCREENING_EVIDENCE', trace_id=trace) from None

    class BoundReads:
        async def call(self, name, arguments):
            response = await business.call(name, arguments)
            if not isinstance(response, dict) or response.get('traceId') != trace:
                raise AgentToolError('INVALID_TOOL_RESPONSE', trace_id=trace)
            return response

    loaded = await load_case_data(task, BoundReads())
    try:
        return InvestigationHandoff(screening=result.model_copy(deep=True), task=task.model_copy(deep=True),
                                    snapshot=loaded['case_snapshot'], messages=loaded['messages'])
    except ValidationError:
        raise AgentToolError('HANDOFF_NOT_READY', trace_id=trace) from None


class Investigator(Protocol):
    async def investigate(self, request: InvestigationHandoff) -> InvestigationDraft: ...


async def run_readonly_investigation(handoff: InvestigationHandoff, investigator: Investigator, *, timeout_seconds=120):
    # Revalidate deserialized/model_copy inputs at the boundary. Callback code is trusted service code.
    try:
        bound = InvestigationHandoff.model_validate(handoff.model_dump(mode='json', by_alias=True))
    except ValidationError:
        raise AgentToolError('HANDOFF_NOT_READY', trace_id=str(handoff.task.trace_id)) from None
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
        raise ValueError('Investigation timeout must be finite and in (0,120]')
    trace = str(bound.task.trace_id)
    try:
        async with asyncio.timeout(timeout_seconds):
            # A callback cannot alter the copy retained for output/evidence validation.
            try:
                draft = await investigator.investigate(bound.model_copy(deep=True))
                draft = InvestigationDraft.model_validate(draft.model_dump(mode='json', by_alias=True)
                                                          if isinstance(draft, InvestigationDraft) else draft)
            except (ValidationError, TypeError, AttributeError):
                raise AgentToolError('INVALID_INVESTIGATION_DRAFT', trace_id=trace) from None
    except TimeoutError:
        raise AgentToolError('INVESTIGATION_TIMEOUT', trace_id=trace, retryable=True) from None
    state = bound.agent_state()
    state['policy_evidence'] = draft.policy_evidence
    result = validate_analysis(state, draft.analysis.model_dump(mode='json', by_alias=True))
    return InvestigationDraft(analysis=result, policy_evidence=draft.policy_evidence,
                              harness_budget=draft.harness_budget)


class ExistingInvestigator:
    """Optional adapter for the current investigation/Harness; no Graph dispatch or Backend writes."""
    def __init__(self, *, model, business, policy, observer=None):
        self.model, self.business, self.policy, self.observer = model, business, policy, observer

    async def investigate(self, request):
        state = request.agent_state()
        patch = await guarded_investigate(state, model=self.model, business=self.business,
                                         policy=self.policy, observer=self.observer)
        if patch.get('task_outcome') or patch.get('error'):
            error = patch.get('error') or {}
            raise AgentToolError(error.get('code', 'INVESTIGATION_FAILED'), trace_id=state['trace_id'])
        analysis = AnalysisResult(caseId=state['case_id'], snapshotId=state['snapshot_id'],
                                  evidenceVersion=state['evidence_version'], policyVersion=state['policy_version'],
                                  messageFindings=patch['message_findings'])
        return InvestigationDraft(analysis=analysis, policyEvidence=patch.get('policy_evidence', []),
                                  harnessBudget=patch.get('harness_budget', {}))
