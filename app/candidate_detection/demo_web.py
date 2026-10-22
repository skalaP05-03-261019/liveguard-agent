"""Loopback-only interactive candidate demo; no Backend or moderation mutations."""

import argparse
import asyncio
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import Headers

from app.clients.business_mcp import AgentToolError
from app.investigation.model import model_from_env
from app.phase2_bridge.contracts import ScreenRequest, ScreenResult
from .contracts import select_candidate
from .screen import CandidateScreen

STATIC = Path(__file__).with_name('demo_static')
MAX_BODY = 16384


def fail(status, code):
    return JSONResponse(status_code=status, content={'error': {'code': code}})


class LocalDemoBoundary:
    """Require local Host and same-origin JSON before reading bounded chat input."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        host = headers.get('host', '')
        name = host.partition(':')[0]
        if name not in {'127.0.0.1', 'localhost'}:
            return await fail(403, 'LOCAL_HOST_REQUIRED')(scope, receive, send)
        if scope.get('path') == '/api/screen':
            if scope.get('method') != 'POST':
                return await fail(405, 'METHOD_NOT_ALLOWED')(scope, receive, send)
            if headers.get('origin') != 'http://' + host:
                return await fail(403, 'SAME_ORIGIN_REQUIRED')(scope, receive, send)
            if headers.get('content-type', '').split(';', 1)[0].lower() != 'application/json':
                return await fail(415, 'JSON_REQUIRED')(scope, receive, send)
            body = bytearray()
            while True:
                event = await receive()
                if event['type'] == 'http.disconnect':
                    return
                chunk = event.get('body', b'')
                if len(body) + len(chunk) > MAX_BODY:
                    return await fail(413, 'REQUEST_TOO_LARGE')(scope, receive, send)
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


def create_app(screen, *, max_concurrent=2):
    if type(max_concurrent) is not int or not 1 <= max_concurrent <= 8:
        raise ValueError('Invalid demo concurrency')
    app = FastAPI(title='LIVE:GUARD local candidate demo',
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalDemoBoundary)
    slots = asyncio.Semaphore(max_concurrent)

    @app.middleware('http')
    async def private_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        return fail(422, 'VALIDATION_FAILED')

    @app.get('/')
    async def page():
        return FileResponse(STATIC/'index.html', media_type='text/html')

    @app.get('/demo.css')
    async def css():
        return FileResponse(STATIC/'demo.css', media_type='text/css')

    @app.get('/demo.js')
    async def script():
        return FileResponse(STATIC/'demo.js', media_type='application/javascript')

    @app.get('/health')
    async def health():
        return {'mode': 'local-candidate-demo', 'modelConfigured': screen is not None,
                'executionAuthorized': False}

    @app.post('/api/screen', response_model=ScreenResult)
    async def assess(request: ScreenRequest):
        if screen is None:
            return fail(503, 'MODEL_NOT_CONFIGURED')
        if slots.locked():
            return fail(429, 'CANDIDATE_BUSY')
        await slots.acquire()
        try:
            try:
                assessment, usage = await screen.assess(request.input, trace_id=str(request.request_id))
            except AgentToolError as error:
                if error.code != 'INVALID_MODEL_OUTPUT':
                    raise
                # The model can return malformed evidence once. Retry the same
                # bounded read-only assessment; never relax provenance checks.
                assessment, usage = await screen.assess(request.input, trace_id=str(request.request_id))
            return ScreenResult(requestId=request.request_id, liveId=request.input.live_id,
                                targetMessageId=request.input.target.message_id,
                                experiment=request.experiment,
                                selected=select_candidate(assessment, request.experiment),
                                assessment=assessment, usage=usage)
        except AgentToolError as error:
            if error.code == 'CANDIDATE_TIMEOUT':
                return fail(504, 'CANDIDATE_TIMEOUT')
            if error.code in {'INVALID_MODEL_OUTPUT', 'FORBIDDEN'}:
                return fail(502, 'MODEL_RESPONSE_REJECTED')
            return fail(503, 'MODEL_UNAVAILABLE')
        except Exception:
            return fail(503, 'MODEL_UNAVAILABLE')
        finally:
            slots.release()

    return app


def read_model_key(path):
    # Parse data only; never source an arbitrary shell file or copy other secrets.
    values = {}
    for line in Path(path).read_text().splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().removeprefix('export ')
        if key in {'MODEL_API_KEY', 'OPENAI_API_KEY'}:
            values[key] = value.strip().strip('"').strip("'")
    key = values.get('MODEL_API_KEY') or values.get('OPENAI_API_KEY')
    if not key or '\n' in key or '\r' in key:
        raise ValueError('No usable model key in env file')
    return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--container', action='store_true',
                        help='Bind inside container; publish the Docker port to host 127.0.0.1 only')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not args.model.strip():
        parser.error('Use a local port in 1024..65535 and an explicit model name')
    try:
        os.environ['MODEL_PROVIDER'] = 'openai'
        os.environ['MODEL_NAME'] = args.model
        os.environ['MODEL_API_KEY'] = read_model_key(args.env_file)
        screen = CandidateScreen(model_from_env())
    except (OSError, ValueError):
        parser.error('Cannot load local model configuration; check env file privately')
    uvicorn.run(create_app(screen), host='0.0.0.0' if args.container else '127.0.0.1',
                port=args.port, log_level='warning', access_log=False)


if __name__ == '__main__':
    main()
