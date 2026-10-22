"""Collaboration boundary tests with controlled model/BE responses; not classification evidence."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import threading
import time
import unittest
import httpx
import uvicorn

from app.candidate_detection.api import create_app, PATH
from app.candidate_detection.screen import CandidateScreen
from app.clients.business_mcp import AgentToolError
from app.investigation.model import ModelTurn, ToolCall
from app.phase2_bridge.contracts import ScreenRequest, ScreenResult, InvestigationDraft, InvestigationHandoff
from app.phase2_bridge.handoff import prepare_handoff, run_readonly_investigation, ExistingInvestigator
from app.schemas import TaskEnvelope

FIXTURE = json.loads((Path(__file__).parents[1]/'examples/phase2-handoff/controlled-flow.json').read_text())
TOKEN = 'phase2-local-test-token'


class Reads:
    def __init__(self, fixture):
        self.fixture, self.calls = fixture, []

    async def call(self, name, arguments):
        self.calls.append(name)
        key = {'get_moderation_case': 'case', 'get_chat_context': 'context'}[name]
        return {'data': deepcopy(self.fixture[key]), 'traceId': arguments['traceId']}


class Model:
    def __init__(self, *, delay=0, fail=False):
        self.calls, self.delay, self.fail = 0, delay, fail

    async def complete(self, messages, tools, *, trace_id):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError('private-error-content')
        return ModelTurn([ToolCall('one', 'report_candidate_signals', FIXTURE['screenResult']['assessment'])], {})


@contextmanager
def tcp_app(app):
    sock = socket.socket(); sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+5
        while not server.started and thread.is_alive() and time.monotonic()<deadline:
            time.sleep(.01)
        if not server.started:
            raise RuntimeError('Standalone server did not start')
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        thread.join(timeout=5); sock.close()
        if thread.is_alive():
            raise RuntimeError('Standalone server did not stop')


class CandidateApiTests(unittest.IsolatedAsyncioTestCase):
    def client(self, model=None, *, token=TOKEN, max_concurrent=2, timeout=8):
        model = model or Model()
        app = create_app(screen=CandidateScreen(model, timeout_seconds=timeout), service_token=token,
                         max_concurrent=max_concurrent)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://candidate.test')

    async def test_authentication_precedes_invalid_or_large_body(self):
        model = Model()
        async with self.client(model) as client:
            for headers in ({}, {'Authorization': 'Bearer wrong'}):
                response = await client.post(PATH, content=b'private-invalid-json'*10000, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertNotIn('private-invalid-json', response.text)
        self.assertEqual(model.calls, 0)

    async def test_valid_response_binding_and_no_business_authority(self):
        async with self.client() as client:
            response = await client.post(PATH, json=FIXTURE['screenRequest'], headers={'Authorization': 'Bearer '+TOKEN})
        self.assertEqual(response.status_code, 200)
        result = ScreenResult.model_validate(response.json())
        self.assertEqual(result.request_id, ScreenRequest.model_validate(FIXTURE['screenRequest']).request_id)
        self.assertFalse(result.execution_authorized)
        self.assertTrue(result.selected)

    async def test_streamed_body_limit_and_safe_validation_errors(self):
        async def chunks():
            yield b'x'*20000
            yield b'x'*20000
        model = Model()
        async with self.client(model) as client:
            headers = {'Authorization': 'Bearer '+TOKEN}
            oversized = await client.post(PATH, content=chunks(), headers=headers)
            self.assertEqual(oversized.status_code, 413)
            invalid = deepcopy(FIXTURE['screenRequest']); invalid['input']['target']['text'] = 'private-'+'x'*501
            response = await client.post(PATH, json=invalid, headers=headers)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('private-', response.text)
        self.assertEqual(model.calls, 0)

    async def test_backpressure_rejects_without_waiting_for_a_slot(self):
        model = Model(delay=.15)
        async with self.client(model, max_concurrent=1) as client:
            async def send():
                return await client.post(PATH, json=FIXTURE['screenRequest'], headers={'Authorization': 'Bearer '+TOKEN})
            first = asyncio.create_task(send())
            while model.calls == 0:
                await asyncio.sleep(.001)
            second = await send()
            self.assertEqual(second.status_code, 429)
            self.assertEqual((await first).status_code, 200)
            self.assertEqual((await send()).status_code, 200)
        self.assertEqual(model.calls, 2)

    async def test_dependency_error_and_timeout_are_not_normal_results(self):
        for model, timeout, expected in ((Model(fail=True), 8, 503), (Model(delay=.1), .01, 504)):
            async with self.client(model, timeout=timeout) as client:
                response = await client.post(PATH, json=FIXTURE['screenRequest'], headers={'Authorization': 'Bearer '+TOKEN})
            self.assertEqual(response.status_code, expected)
            self.assertNotIn('private-error-content', response.text)
            self.assertNotIn('selected', response.json())

    async def test_missing_configuration_does_not_run_models(self):
        for app, code in ((create_app(), 'SERVICE_AUTH_NOT_CONFIGURED'),
                          (create_app(service_token=TOKEN), 'CANDIDATE_NOT_CONFIGURED')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                response = await client.post(PATH, json=FIXTURE['screenRequest'], headers={'Authorization': 'Bearer '+TOKEN})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()['error']['code'], code)
                self.assertFalse((await client.get('/health')).json()['configured'])

    @unittest.skipUnless(os.getenv('LIVEGUARD_TEST_TCP') == '1', 'TCP opt-in')
    async def test_standalone_api_over_real_tcp(self):
        model = Model()
        with tcp_app(create_app(screen=CandidateScreen(model), service_token=TOKEN)) as base:
            async with httpx.AsyncClient(base_url=base) as client:
                self.assertEqual((await client.post(PATH, json=FIXTURE['screenRequest'])).status_code, 401)
                response = await client.post(PATH, json=FIXTURE['screenRequest'], headers={'Authorization': 'Bearer '+TOKEN})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()['executionAuthorized'])
        self.assertEqual(model.calls, 1)


class HandoffTests(unittest.IsolatedAsyncioTestCase):
    async def prepare(self, fixture=None, reads=None):
        f = fixture or deepcopy(FIXTURE)
        return await prepare_handoff(task=TaskEnvelope.model_validate(f['task']),
            request=ScreenRequest.model_validate(f['screenRequest']), result=ScreenResult.model_validate(f['screenResult']),
            business=reads or Reads(f))

    async def test_prepare_binds_backend_task_snapshot_scope_and_signals(self):
        reads = Reads(deepcopy(FIXTURE))
        handoff = await self.prepare(reads=reads)
        self.assertEqual(reads.calls, ['get_moderation_case', 'get_chat_context'])
        self.assertEqual(handoff.snapshot.policy_version, 'fixture-policy-v1')
        self.assertEqual(handoff.snapshot.handling_route, 'CONTEXT_REVIEW')
        self.assertFalse(handoff.execution_authorized)
        self.assertEqual(len(handoff.agent_state()['target_messages']), 1)

    async def test_receipt_identity_or_unselected_stops_before_backend_reads(self):
        for change in ('requestId', 'liveId', 'targetMessageId', 'unselected'):
            f = deepcopy(FIXTURE)
            if change == 'unselected':
                f['screenResult']['selected'] = False
                f['screenResult']['assessment'] = {'signals': [], 'missingFacts': []}
            else:
                f['screenResult'][change] = '00000000-0000-4000-8000-000000000099'
            reads = Reads(f)
            with self.subTest(change=change), self.assertRaises(AgentToolError):
                await self.prepare(f, reads)
            self.assertEqual(reads.calls, [])

    async def test_stale_case_scope_route_visibility_and_changed_quotes_fail_closed(self):
        changes = [('snapshot', lambda f: f['case'].update(evidenceVersion=2)),
                   ('live', lambda f: f['case'].update(liveId='00000000-0000-4000-8000-000000000099')),
                   ('route', lambda f: f['case'].update(handlingRoute='RULE_CONFIRMED')),
                   ('confirmed', lambda f: f['case'].update(confirmationStatus='CONFIRMED_VIOLATION')),
                   ('public', lambda f: f['context']['messages'][0].update(visibility='PUBLIC')),
                   ('mask', lambda f: f['context']['messages'][0].update(text='[MASKED]'))]
        for name, change in changes:
            f = deepcopy(FIXTURE); change(f)
            with self.subTest(change=name), self.assertRaises(AgentToolError):
                await self.prepare(f)

    async def test_wrong_trace_is_rejected(self):
        class WrongTrace(Reads):
            async def call(self, name, arguments):
                response = await super().call(name, arguments)
                return {**response, 'traceId': 'foreign'}
        with self.assertRaises(AgentToolError) as error:
            await self.prepare(reads=WrongTrace(deepcopy(FIXTURE)))
        self.assertEqual(error.exception.code, 'INVALID_TOOL_RESPONSE')

    async def test_valid_uncertain_output_is_readonly(self):
        class Investigator:
            async def investigate(self, request):
                return InvestigationDraft.model_validate(FIXTURE['draft'])
        handoff = await self.prepare()
        draft = await run_readonly_investigation(handoff, Investigator())
        self.assertEqual(draft.analysis.message_findings[0].verdict, 'NEEDS_REVIEW')
        self.assertFalse(draft.execution_authorized)

    async def test_investigator_cannot_change_bound_case_or_invent_categories(self):
        class WrongCase:
            async def investigate(self, request):
                draft = deepcopy(FIXTURE['draft'])
                draft['analysis']['caseId'] = '00000000-0000-4000-8000-000000000099'
                return draft
        class NewCategory:
            async def investigate(self, request):
                draft = deepcopy(FIXTURE['draft'])
                draft['analysis']['messageFindings'][0].update(verdict='VIOLATION',categories=['REGION_DISPARAGEMENT'])
                return draft
        class MutateInput:
            async def investigate(self, request):
                request.snapshot.policy_version = 'made-up'
                draft = deepcopy(FIXTURE['draft']); draft['analysis']['policyVersion'] = 'made-up'
                return draft
        handoff = await self.prepare()
        for callback in (WrongCase(), NewCategory(), MutateInput()):
            with self.subTest(callback=type(callback).__name__), self.assertRaises(AgentToolError):
                await run_readonly_investigation(handoff, callback)
        self.assertEqual(handoff.snapshot.policy_version, 'fixture-policy-v1')

    async def test_callback_timeout_is_explicit(self):
        class Slow:
            async def investigate(self, request):
                await asyncio.sleep(.1)
        with self.assertRaises(AgentToolError) as error:
            await run_readonly_investigation(await self.prepare(), Slow(), timeout_seconds=.01)
        self.assertEqual(error.exception.code, 'INVESTIGATION_TIMEOUT')

    async def test_existing_investigation_and_harness_adapter(self):
        from test_investigation import ScriptedModel, Policies, finish
        f = deepcopy(FIXTURE)
        model = ScriptedModel([finish(f['draft']['analysis']['messageFindings'])])
        adapter = ExistingInvestigator(model=model,business=Reads(f),policy=Policies(),observer=lambda event: None)
        draft = await run_readonly_investigation(await self.prepare(), adapter)
        self.assertEqual(draft.analysis.message_findings[0].verdict, 'NEEDS_REVIEW')
        self.assertEqual(draft.harness_budget['modelCalls'], 1)
