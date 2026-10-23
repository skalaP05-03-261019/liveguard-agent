"""Standalone opt-in investigation drafts. Never submits analysis or starts approval/Worker Graphs."""

import asyncio
import os
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from app.candidate_detection.api import RequestBoundary, failure
from app.clients.business_mcp import AgentToolError
from .service import DraftRequest, DraftResponse, service_from_env

PATH = '/agent/investigations/draft'

CONFLICTS = frozenset({'SCREENING_BINDING_MISMATCH', 'INVALID_TASK_KIND',
                      'INVALID_SCREENING_EVIDENCE', 'HANDOFF_NOT_READY', 'STALE_EVIDENCE',
                      'INVALID_EVIDENCE', 'POLICY_MISMATCH'})


def create_app(*, service=None, service_token='', max_concurrent=1):
    if type(max_concurrent) is not int or not 1 <= max_concurrent <= 16:
        raise ValueError('Concurrency must be an integer in 1..16')
    app = FastAPI(title='LIVE:GUARD Phase 2 Investigation Drafts', version='0.1.0',
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestBoundary, token=service_token, prefix='/agent/investigations',
                       body_path=PATH, max_body_bytes=65536)
    slots = asyncio.Semaphore(max_concurrent)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return failure(422, 'VALIDATION_FAILED')

    @app.get('/health')
    async def health():
        return {'configured': service is not None and bool(service_token.strip()),
                'mode': 'standalone-investigation-draft', 'executionAuthorized': False}

    @app.post(PATH, response_model=DraftResponse)
    async def draft(request: DraftRequest):
        request_id = request.screening_request.request_id
        if service is None:
            return failure(503, 'INVESTIGATION_NOT_CONFIGURED', request_id)
        if slots.locked():
            return failure(429, 'INVESTIGATION_BUSY', request_id)
        await slots.acquire()
        try:
            return await service.run(request)
        except AgentToolError as error:
            if error.code in CONFLICTS:
                return failure(409, error.code, request_id)
            if error.code == 'INVESTIGATION_TIMEOUT':
                return failure(504, error.code, request_id)
            if error.code == 'EXECUTION_LIMIT':
                return failure(503, 'INVESTIGATION_BUDGET_EXHAUSTED', request_id)
            if error.code in {'INVALID_MODEL_OUTPUT', 'INVALID_INVESTIGATION_DRAFT',
                              'INVALID_POLICY_EVIDENCE', 'INVALID_TOOL_RESPONSE'}:
                return failure(502, 'INVESTIGATION_RESPONSE_REJECTED', request_id)
            return failure(503, 'INVESTIGATION_DEPENDENCY_UNAVAILABLE', request_id)
        except Exception:
            return failure(503, 'INVESTIGATION_DEPENDENCY_UNAVAILABLE', request_id)
        finally:
            slots.release()

    return app


def configured_app():
    service = None
    if os.getenv('INVESTIGATION_DRAFT_API_ENABLED') == 'true':
        try:
            service = service_from_env()
        except ValueError:
            pass
    return create_app(service=service, service_token=os.getenv('INVESTIGATION_DRAFT_SERVICE_TOKEN', ''))


app = configured_app()
