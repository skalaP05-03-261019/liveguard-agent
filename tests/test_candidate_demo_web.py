"""Local UI boundary and real CandidateScreen contract with synthetic chat only."""

import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

import httpx

from app.candidate_detection.contracts import Assessment
from app.candidate_detection.demo_web import create_app, read_model_key
from app.clients.business_mcp import AgentToolError


class Screen:
    def __init__(self, result=None, error=None):
        self.result = result or Assessment()
        self.error = error
        self.calls = []

    async def assess(self, data, *, trace_id):
        self.calls.append((data, trace_id))
        if self.error: raise self.error
        return self.result, {'inputTokens': 17, 'outputTokens': 4}


def payload(text='Synthetic ordinary message', context=()):
    return {'requestId': str(uuid4()), 'experiment': 'clear-or-possible',
            'input': {'liveId': str(uuid4()),
                      'target': {'messageId': str(uuid4()), 'text': text},
                      'context': list(context)}}


class DemoWebTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, app, method, path, *, json=None, headers=None, content=None):
        headers = {'Origin': 'http://127.0.0.1:8765', **(headers or {})}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1:8765') as client:
            return await client.request(method, path, json=json, headers=headers, content=content)

    async def test_page_is_local_static_and_has_input_and_evidence_view(self):
        app = create_app(Screen())
        page = await self.request(app, 'GET', '/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('채팅을 입력해 보세요', page.text)
        self.assertIn('id="analysis-content"', page.text)
        self.assertIn('no-store', page.headers['cache-control'])
        self.assertIn("script-src 'self'", page.headers['content-security-policy'])
        script = await self.request(app, 'GET', '/demo.js')
        self.assertIn("fetch('/api/screen'", script.text)
        self.assertIn("experiment: 'include-uncertain'", script.text)
        self.assertIn('정상 · 후보 신호 없음', script.text)
        self.assertNotIn('운영자 판단 대기', script.text)
        self.assertIn('textContent', script.text)
        self.assertNotIn('innerHTML', script.text)
        self.assertEqual((await self.request(app, 'GET', '/health')).json()['executionAuthorized'], False)

    async def test_chat_and_context_reach_screen_without_mutation(self):
        screen = Screen(); app = create_app(screen)
        previous = {'messageId': str(uuid4()), 'text': 'Earlier synthetic chat'}
        body = payload('Synthetic region phrase', [previous])
        result = await self.request(app, 'POST', '/api/screen', json=body)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(result.json()['selected'])
        self.assertFalse(result.json()['executionAuthorized'])
        self.assertEqual(result.json()['targetMessageId'], body['input']['target']['messageId'])
        self.assertEqual(screen.calls[0][0].context[0].text, previous['text'])
        self.assertEqual(str(screen.calls[0][0].target.message_id), body['input']['target']['messageId'])
        self.assertEqual(body['input']['target']['text'], 'Synthetic region phrase')

    async def test_model_signal_and_exact_evidence_are_visible(self):
        body = payload('Synthetic target message')
        signal = {'signals': [{'kind': 'TARGETED_ATTACK', 'strength': 'CLEAR',
                   'reason': 'Synthetic local reason',
                   'evidence': [{'messageId': body['input']['target']['messageId'],
                                 'text': 'Synthetic target message'}]}]}
        app = create_app(Screen(Assessment.model_validate(signal)))
        result = await self.request(app, 'POST', '/api/screen', json=body)
        self.assertTrue(result.json()['selected'])
        self.assertEqual(result.json()['assessment']['signals'][0]['reason'], 'Synthetic local reason')

    async def test_uncertain_signal_is_candidate_in_binary_demo_experiment(self):
        body = payload('Synthetic ambiguous target')
        body['experiment'] = 'include-uncertain'
        signal = {'signals': [{'kind': 'TARGETED_ATTACK', 'strength': 'UNCERTAIN',
                   'reason': 'Synthetic target may need review',
                   'evidence': [{'messageId': body['input']['target']['messageId'],
                                 'text': 'Synthetic ambiguous target'}]}],
                  'missingFacts': ['Synthetic missing context']}
        result = await self.request(create_app(Screen(Assessment.model_validate(signal))),
                                    'POST', '/api/screen', json=body)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['selected'])
        self.assertEqual(result.json()['assessment']['missingFacts'], ['Synthetic missing context'])
        self.assertFalse(result.json()['executionAuthorized'])

    async def test_cross_origin_host_content_type_and_body_limit_are_rejected_before_screen(self):
        screen = Screen(); app = create_app(screen)
        body = payload()
        for headers, expected in [({'Origin': 'http://evil.example'}, 403),
                                  ({'Host': 'evil.example'}, 403)]:
            result = await self.request(app, 'POST', '/api/screen', json=body, headers=headers)
            self.assertEqual(result.status_code, expected)
        result = await self.request(app, 'POST', '/api/screen', content='{}',
                                    headers={'Content-Type': 'text/plain'})
        self.assertEqual(result.status_code, 415)
        result = await self.request(app, 'POST', '/api/screen',
                                    content=b'x'*16385,
                                    headers={'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 413)
        self.assertFalse(screen.calls)

    async def test_invalid_chat_does_not_echo_content(self):
        screen = Screen(); app = create_app(screen)
        long_text = 'PRIVATE_SYNTHETIC_'+('x'*500)
        result = await self.request(app, 'POST', '/api/screen', json=payload(long_text))
        self.assertEqual(result.status_code, 422)
        self.assertNotIn('PRIVATE_SYNTHETIC', result.text)
        self.assertFalse(screen.calls)

    async def test_dependency_error_is_redacted(self):
        class BrokenScreen:
            async def assess(self, data, *, trace_id):
                raise RuntimeError('PRIVATE_SYNTHETIC_MODEL_BODY')
        result = await self.request(create_app(BrokenScreen()), 'POST', '/api/screen', json=payload())
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json(), {'error': {'code': 'MODEL_UNAVAILABLE'}})
        self.assertNotIn('PRIVATE_SYNTHETIC', result.text)

    async def test_invalid_model_evidence_retries_once_with_same_chat_and_then_succeeds(self):
        body = payload('Synthetic target chat', [
            {'messageId': str(uuid4()), 'text': 'Synthetic preceding chat'}])

        class IntermittentScreen:
            def __init__(self): self.calls = []

            async def assess(self, data, *, trace_id):
                self.calls.append((data, trace_id))
                if len(self.calls) == 1:
                    raise AgentToolError('INVALID_MODEL_OUTPUT', trace_id=trace_id)
                return Assessment(), {'inputTokens': 10, 'outputTokens': 3}

        screen = IntermittentScreen()
        response = await self.request(create_app(screen), 'POST', '/api/screen', json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(screen.calls), 2)
        self.assertEqual(screen.calls[0], screen.calls[1])
        self.assertEqual(screen.calls[0][0].context[0].text, 'Synthetic preceding chat')

    async def test_persistent_invalid_model_evidence_stops_after_one_retry(self):
        body = payload()

        class InvalidScreen:
            calls = 0

            async def assess(self, data, *, trace_id):
                self.calls += 1
                raise AgentToolError('INVALID_MODEL_OUTPUT', trace_id=trace_id)

        screen = InvalidScreen()
        response = await self.request(create_app(screen), 'POST', '/api/screen', json=body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {'error': {'code': 'MODEL_RESPONSE_REJECTED'}})
        self.assertEqual(screen.calls, 2)

    async def test_auth_failure_does_not_retry(self):
        class UnauthorizedScreen:
            calls = 0

            async def assess(self, data, *, trace_id):
                self.calls += 1
                raise AgentToolError('UNAUTHENTICATED', trace_id=trace_id)

        screen = UnauthorizedScreen()
        response = await self.request(create_app(screen), 'POST', '/api/screen', json=payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(screen.calls, 1)

    async def test_no_model_configured_returns_available_page_and_api_error(self):
        app = create_app(None)
        self.assertEqual((await self.request(app, 'GET', '/')).status_code, 200)
        result = await self.request(app, 'POST', '/api/screen', json=payload())
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()['error']['code'], 'MODEL_NOT_CONFIGURED')

    def test_env_reader_takes_only_key_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'.env'
            path.write_text('OTHER_SECRET=must-not-load\nOPENAI_API_KEY="synthetic-key"\n')
            self.assertEqual(read_model_key(path), 'synthetic-key')
            path.write_text('OTHER_SECRET=must-not-load\n')
            with self.assertRaises(ValueError): read_model_key(path)
