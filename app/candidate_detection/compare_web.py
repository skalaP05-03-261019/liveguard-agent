"""Side-by-side local experiment: GPT candidate screening vs UnSmile pickle."""

import argparse
import asyncio
import os
import time
from pathlib import Path

import uvicorn
from fastapi import Request
from fastapi.responses import FileResponse

from app.clients.business_mcp import AgentToolError
from app.investigation.model import model_from_env
from app.phase2_bridge.contracts import ScreenRequest
from .contracts import select_candidate
from .demo_web import create_app, fail, read_model_key
from .screen import CandidateScreen
from .unsmile_pickle import UnSmilePickle

STATIC = Path(__file__).with_name('demo_static')


def create_comparison_app(screen, unsmile):
    app = create_app(screen)
    slots = asyncio.Semaphore(2)

    @app.get('/compare')
    async def page():
        return FileResponse(STATIC / 'compare.html', media_type='text/html')

    @app.get('/compare.js')
    async def script():
        return FileResponse(STATIC / 'compare.js', media_type='application/javascript')

    @app.get('/compare.css')
    async def style():
        return FileResponse(STATIC / 'compare.css', media_type='text/css')

    @app.get('/api/compare/health')
    async def health():
        return {'gptConfigured': screen is not None, 'unsmileConfigured': unsmile is not None,
                'executionAuthorized': False}

    @app.post('/api/compare')
    async def compare(request: ScreenRequest):
        if screen is None and unsmile is None:
            return fail(503, 'MODELS_NOT_CONFIGURED')
        if slots.locked():
            return fail(429, 'CANDIDATE_BUSY')
        await slots.acquire()
        try:
            async def gpt_result():
                if screen is None:
                    return {'status': 'NOT_CONFIGURED'}
                started = time.perf_counter()
                try:
                    try:
                        assessment, usage = await screen.assess(request.input, trace_id=str(request.request_id))
                    except AgentToolError as error:
                        if error.code != 'INVALID_MODEL_OUTPUT':
                            raise
                        assessment, usage = await screen.assess(request.input, trace_id=str(request.request_id))
                    return {'status': 'OK', 'selected': select_candidate(assessment, request.experiment),
                            'signals': [s.model_dump(mode='json', by_alias=True) for s in assessment.signals],
                            'usage': usage, 'elapsedMs': round((time.perf_counter() - started) * 1000, 2),
                            'scope': 'target-and-context'}
                except Exception:
                    return {'status': 'UNAVAILABLE', 'errorCode': 'GPT_SCREEN_FAILED',
                            'elapsedMs': round((time.perf_counter() - started) * 1000, 2)}

            async def unsmile_result():
                if unsmile is None:
                    return {'status': 'NOT_CONFIGURED'}
                started = time.perf_counter()
                try:
                    result = await asyncio.to_thread(unsmile.predict, request.input.target.text)
                    return {'status': 'OK', **result,
                            'elapsedMs': round((time.perf_counter() - started) * 1000, 2)}
                except Exception:
                    return {'status': 'UNAVAILABLE', 'errorCode': 'UNSMILE_INFERENCE_FAILED',
                            'elapsedMs': round((time.perf_counter() - started) * 1000, 2)}

            gpt, local = await asyncio.gather(gpt_result(), unsmile_result())
            return {'requestId': str(request.request_id), 'experiment': request.experiment,
                    'gpt': gpt, 'unsmile': local, 'executionAuthorized': False}
        finally:
            slots.release()

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--unsmile-pkl', type=Path, required=True)
    parser.add_argument('--tokenizer', default='beomi/kcbert-base')
    parser.add_argument('--trust-local-pickle', action='store_true')
    parser.add_argument('--threshold', type=float, default=0.5)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--model', help='Existing OpenAI model name for GPT comparison')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Use a local port in 1024..65535')
    if bool(args.env_file) != bool(args.model):
        parser.error('Provide both --env-file and --model for GPT comparison')
    try:
        local = UnSmilePickle.load(args.unsmile_pkl, args.tokenizer, threshold=args.threshold,
                                   trust_local_pickle=args.trust_local_pickle)
        screen = None
        if args.env_file:
            os.environ['MODEL_PROVIDER'] = 'openai'
            os.environ['MODEL_NAME'] = args.model
            os.environ['MODEL_API_KEY'] = read_model_key(args.env_file)
            screen = CandidateScreen(model_from_env())
    except (OSError, ValueError, ImportError) as error:
        parser.error(str(error))
    uvicorn.run(create_comparison_app(screen, local), host='127.0.0.1', port=args.port,
                log_level='warning', access_log=False)


if __name__ == '__main__':
    main()
