"""Standalone opt-in candidate API. Never mounted in the existing task/Worker application."""

import asyncio
import hmac
import os
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from app.clients.business_mcp import AgentToolError
from app.investigation.model import model_from_env
from app.phase2_bridge.contracts import ScreenRequest, ScreenResult
from .contracts import select_candidate
from .screen import CandidateScreen

PATH = '/agent/candidates/screen'


def failure(status, code, request_id=None):
    content = {'error': {'code': code}}
    if request_id is not None:
        content['requestId'] = str(request_id)
    return JSONResponse(status_code=status, content=content)


class RequestBoundary:
    """Authenticate before reading chat; bound streamed bodies before JSON parsing."""
    def __init__(self, app, *, token):
        self.app, self._token = app, token

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/agent/candidates'):
            return await self.app(scope, receive, send)
        expected = self._token
        if not expected.strip():
            return await failure(503, 'SERVICE_AUTH_NOT_CONFIGURED')(scope, receive, send)
        authorization = Headers(scope=scope).get('authorization', '')
        if not authorization.startswith('Bearer ') or not hmac.compare_digest(
                authorization[7:].encode(), expected.encode()):
            return await failure(401, 'UNAUTHENTICATED')(scope, receive, send)
        if scope.get('path') == PATH and scope.get('method') == 'POST':
            body = bytearray()
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return
                chunk = event.get('body', b'')
                if len(body) + len(chunk) > 32768:
                    return await failure(413, 'REQUEST_TOO_LARGE')(scope, receive, send)
                body.extend(chunk)
                if not event.get('more_body', False):
                    break
            delivered = False

            async def bounded_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()

            return await self.app(scope, bounded_receive, send)
        return await self.app(scope, receive, send)


def create_app(*, screen=None, service_token='', max_concurrent=2):
    if type(max_concurrent) is not int or not 1 <= max_concurrent <= 16:
        raise ValueError('Concurrency must be an integer in 1..16')
    application = FastAPI(title='LIVE:GUARD Phase 2 Candidate API', version='0.1.0',
                          docs_url=None, redoc_url=None, openapi_url=None)
    slots = asyncio.Semaphore(max_concurrent)
    application.add_middleware(RequestBoundary, token=service_token)

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # Default FastAPI errors may include the input chat. Return only a stable code.
        return failure(422, 'VALIDATION_FAILED')

    @application.get('/health')
    async def health():
        return {'configured': screen is not None and bool(service_token.strip()),
                'mode': 'standalone-candidate', 'executionAuthorized': False}

    @application.post(PATH, response_model=ScreenResult)
    async def assess(request: ScreenRequest):
        if screen is None:
            return failure(503, 'CANDIDATE_NOT_CONFIGURED', request.request_id)
        # No waiting queue: checking/acquiring has no suspension while a slot is available.
        if slots.locked():
            return failure(429, 'CANDIDATE_BUSY', request.request_id)
        await slots.acquire()
        try:
            assessment, usage = await screen.assess(request.input, trace_id=str(request.request_id))
            return ScreenResult(requestId=request.request_id, liveId=request.input.live_id,
                                targetMessageId=request.input.target.message_id, experiment=request.experiment,
                                selected=select_candidate(assessment, request.experiment),
                                assessment=assessment, usage=usage)
        except AgentToolError as error:
            code = error.code
            if code == 'CANDIDATE_TIMEOUT':
                return failure(504, code, request.request_id)
            if code in {'INVALID_MODEL_OUTPUT', 'FORBIDDEN'}:
                return failure(502, 'MODEL_RESPONSE_REJECTED', request.request_id)
            return failure(503, 'CANDIDATE_DEPENDENCY_UNAVAILABLE', request.request_id)
        except Exception:
            return failure(503, 'CANDIDATE_DEPENDENCY_UNAVAILABLE', request.request_id)
        finally:
            slots.release()

    return application


def configured_app():
    screen = None
    if os.getenv('CANDIDATE_API_ENABLED') == 'true':
        try:
            screen = CandidateScreen(model_from_env())
        except ValueError:
            pass
    return create_app(screen=screen, service_token=os.getenv('CANDIDATE_SERVICE_TOKEN', ''))


app = configured_app()
