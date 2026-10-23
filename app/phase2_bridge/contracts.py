"""Agent collaboration contracts. New screening interface is not an agreed BE OpenAPI."""

from typing import Any, Literal
from uuid import UUID
from pydantic import Field, model_validator
from app.schemas import AnalysisResult, Contract, TaskEnvelope
from app.case_loader import CaseSnapshot, ChatMessage
from app.candidate_detection.contracts import Assessment, ScreeningInput, select_candidate

Experiment = Literal['clear-only', 'clear-or-possible', 'include-uncertain']


class ScreenRequest(Contract):
    contract_version: Literal['phase2-screen-v1'] = 'phase2-screen-v1'
    request_id: UUID
    experiment: Experiment
    input: ScreeningInput


class ScreenResult(Contract):
    contract_version: Literal['phase2-screen-v1'] = 'phase2-screen-v1'
    request_id: UUID
    live_id: UUID
    target_message_id: UUID
    experiment: Experiment
    selected: bool = Field(strict=True)
    assessment: Assessment
    usage: dict[str, int] = Field(default_factory=dict)
    execution_authorized: Literal[False] = False

    @model_validator(mode='after')
    def selection_is_derived(self):
        if self.selected != select_candidate(self.assessment, self.experiment):
            raise ValueError('Selection must match the declared experiment')
        if any(k not in {'inputTokens', 'outputTokens'} or type(v) is not int or v < 0 for k, v in self.usage.items()):
            raise ValueError('Only nonnegative token counts may be returned')
        return self


class InvestigationHandoff(Contract):
    contract_version: Literal['phase2-investigation-v1'] = 'phase2-investigation-v1'
    screening: ScreenResult
    task: TaskEnvelope
    snapshot: CaseSnapshot
    messages: list[ChatMessage] = Field(min_length=1)
    execution_authorized: Literal[False] = False

    def agent_state(self):
        snapshot = self.snapshot
        targets = {str(mid) for mid in snapshot.target_message_ids}
        messages = [m.model_dump(mode='json', by_alias=True) for m in self.messages]
        return {'task': self.task.model_copy(deep=True), 'case_id': str(self.task.case_id),
                'task_id': str(self.task.task_id), 'thread_id': self.task.thread_id,
                'trace_id': str(self.task.trace_id), 'snapshot_id': str(snapshot.snapshot_id),
                'evidence_version': snapshot.evidence_version, 'policy_version': snapshot.policy_version,
                'live_id': str(snapshot.live_id), 'user_id': str(snapshot.user_id),
                'handling_route': snapshot.handling_route,
                'case_snapshot': snapshot.model_dump(mode='json', by_alias=True),
                'messages': messages, 'target_messages': [m for m in messages if m['messageId'] in targets],
                'context_messages': [m for m in messages if m['messageId'] not in targets],
                'tool_observations': []}

    @model_validator(mode='after')
    def validate_binding(self):
        t, s, r = self.task, self.snapshot, self.screening
        if t.kind not in {'ANALYZE', 'REANALYZE'}:
            raise ValueError('Read-only investigation requires an analysis task')
        if (t.case_id != s.case_id or t.snapshot_id != s.snapshot_id
                or t.expected_evidence_version != s.evidence_version):
            raise ValueError('Backend task and immutable case must match')
        if (not s.user_id or s.live_id != r.live_id
                or s.handling_route != 'CONTEXT_REVIEW'
                or s.confirmation_status not in {'UNCONFIRMED', 'PENDING'}):
            raise ValueError('Only Backend-held, unconfirmed CONTEXT cases are admissible')
        if r.target_message_id not in s.target_message_ids:
            raise ValueError('Screened message must be an explicit Backend target')
        ids = [m.message_id for m in self.messages]
        if len(ids) != len(set(ids)) or not set(s.target_message_ids) <= set(ids):
            raise ValueError('Backend messages must uniquely cover every target')
        by_id = {m.message_id: m for m in self.messages}
        if any(by_id[mid].visibility != 'HELD' for mid in s.target_message_ids):
            raise ValueError('Context targets must be HELD')
        if s.targets is not None and any(by_id[x.message_id].visibility != x.visibility for x in s.targets):
            raise ValueError('Snapshot and context visibility differ')
        # Candidate metadata never replaces BE evidence or becomes a policy finding.
        for signal in r.assessment.signals:
            if not any(q.message_id == r.target_message_id for q in signal.evidence):
                raise ValueError('Each hint requires screened target evidence')
            for quote in signal.evidence:
                if not quote.text.strip() or quote.message_id not in by_id or quote.text not in by_id[quote.message_id].text:
                    raise ValueError('Screening hints changed or are outside frozen BE evidence')
        return self


class InvestigationDraft(Contract):
    analysis: AnalysisResult
    policy_evidence: list[dict[str, Any]] = Field(default_factory=list)
    harness_budget: dict[str, Any] = Field(default_factory=dict)
    execution_authorized: Literal[False] = False
