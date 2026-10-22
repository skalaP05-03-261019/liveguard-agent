"""Read-only service orchestration; HTTP input cannot select executable callbacks or MCP URLs."""

import asyncio
import math
from typing import Literal
from uuid import UUID
from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.investigation.model import model_from_env
from app.schemas import Contract, TaskEnvelope
from .contracts import ScreenRequest, ScreenResult, InvestigationDraft
from .handoff import ExistingInvestigator, prepare_handoff, run_readonly_investigation


class DraftRequest(Contract):
    contract_version: Literal['phase2-draft-v1'] = 'phase2-draft-v1'
    task: TaskEnvelope
    screening_request: ScreenRequest
    screening_result: ScreenResult


class DraftResponse(Contract):
    contract_version: Literal['phase2-draft-v1'] = 'phase2-draft-v1'
    request_id: UUID
    task_id: UUID
    trace_id: UUID
    live_id: UUID
    draft: InvestigationDraft
    execution_authorized: Literal[False] = False


class DraftService:
    def __init__(self, *, business, investigator, timeout_seconds=120):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 120:
            raise ValueError('Draft deadline must be finite and in (0,120]')
        self.business, self.investigator, self.timeout_seconds = business, investigator, timeout_seconds

    async def run(self, request: DraftRequest) -> DraftResponse:
        # Fail before any dependency I/O if an in-process caller bypassed validation.
        request = DraftRequest.model_validate(request.model_dump(mode='json', by_alias=True))
        try:
            async with asyncio.timeout(self.timeout_seconds):
                handoff = await prepare_handoff(task=request.task, request=request.screening_request,
                    result=request.screening_result, business=self.business)
                draft = await run_readonly_investigation(handoff, self.investigator,
                                                        timeout_seconds=self.timeout_seconds)
        except TimeoutError:
            raise AgentToolError('INVESTIGATION_TIMEOUT', trace_id=str(request.task.trace_id)) from None
        return DraftResponse(requestId=request.screening_request.request_id, taskId=request.task.task_id,
                             traceId=request.task.trace_id, liveId=handoff.snapshot.live_id, draft=draft)


def service_from_env():
    business = BusinessMcpReadClient(BusinessMcpSettings.from_env())
    policy = RagMcpReadClient(RagMcpSettings.from_env())
    model = model_from_env()
    return DraftService(business=business,
                        investigator=ExistingInvestigator(model=model, business=business, policy=policy))
