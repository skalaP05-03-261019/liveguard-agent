"""Draft transport/orchestration with controlled evidence; not model quality measurements."""

import asyncio
from copy import deepcopy
import os
import unittest
from unittest.mock import patch
import httpx
from app.clients.business_mcp import AgentToolError
from app.phase2_bridge.api import create_app, configured_app, PATH
from app.phase2_bridge.handoff import ExistingInvestigator
from app.phase2_bridge.service import DraftRequest, DraftResponse, DraftService
from test_phase2_bridge import FIXTURE, Reads, tcp_app
from test_investigation import ScriptedModel, Policies, finish, search, POLICY

TOKEN = 'controlled-draft-test-token'


def payload():
    return {'contractVersion': 'phase2-draft-v1', 'task': deepcopy(FIXTURE['task']),
            'screeningRequest': deepcopy(FIXTURE['screenRequest']),
            'screeningResult': deepcopy(FIXTURE['screenResult'])}


def service(*, fixture=None, timeout=120, model=None, policy=None):
    reads = Reads(deepcopy(fixture or FIXTURE))
    model = model or ScriptedModel([finish(FIXTURE['draft']['analysis']['messageFindings'])])
    investigator = ExistingInvestigator(model=model, business=reads, policy=policy or Policies(),
                                      observer=lambda event: None)
    return DraftService(business=reads, investigator=investigator, timeout_seconds=timeout), reads, model


class DraftServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_joins_be_reads_harness_and_validated_draft(self):
        runner, reads, model = service()
        result = await runner.run(DraftRequest.model_validate(payload()))
        self.assertIsInstance(result, DraftResponse)
        self.assertEqual(reads.calls, ['get_moderation_case', 'get_chat_context'])
        self.assertEqual(result.task_id, DraftRequest.model_validate(payload()).task.task_id)
        self.assertFalse(result.execution_authorized)
        self.assertFalse(result.draft.execution_authorized)
        self.assertEqual(result.draft.harness_budget['modelCalls'], 1)
        self.assertEqual(result.draft.analysis.message_findings[0].verdict, 'NEEDS_REVIEW')

    async def test_policy_search_resource_and_draft_validation_reused(self):
        f = deepcopy(FIXTURE)
        f['case']['policyVersion'] = POLICY['version']
        finding = deepcopy(f['draft']['analysis']['messageFindings'])
        finding[0]['policyIds'] = [POLICY['policyId']]
        policy = Policies()
        runner, reads, _ = service(fixture=f, model=ScriptedModel([search(), finish(finding)]), policy=policy)
        result = await runner.run(DraftRequest.model_validate(payload()))
        self.assertEqual(result.draft.analysis.policy_version, POLICY['version'])
        self.assertEqual(result.draft.harness_budget['modelCalls'], 2)
        self.assertTrue(any(name == 'resource' for name, args in policy.calls))
        self.assertEqual(result.draft.policy_evidence[0]['policyId'], POLICY['policyId'])

    async def test_mismatched_screening_never_reads_be(self):
        p = payload(); p['screeningResult']['requestId'] = '00000000-0000-4000-8000-000000000099'
        runner, reads, model = service()
        with self.assertRaises(AgentToolError) as error:
            await runner.run(DraftRequest.model_validate(p))
        self.assertEqual(error.exception.code, 'SCREENING_BINDING_MISMATCH')
        self.assertEqual(reads.calls, []); self.assertEqual(model.inputs, [])

    async def test_stale_backend_never_calls_investigator(self):
        f = deepcopy(FIXTURE); f['case']['evidenceVersion'] += 1
        runner, reads, model = service(fixture=f)
        with self.assertRaises(AgentToolError) as error:
            await runner.run(DraftRequest.model_validate(payload()))
        self.assertEqual(error.exception.code, 'STALE_EVIDENCE')
        self.assertEqual(model.inputs, [])

    async def test_deadline_includes_initial_backend_loading(self):
        class Slow:
            async def call(self, name, args):
                await asyncio.sleep(1)
        runner, _, model = service(timeout=.01)
        runner.business = Slow()
        with self.assertRaises(AgentToolError) as error:
            await runner.run(DraftRequest.model_validate(payload()))
        self.assertEqual(error.exception.code, 'INVESTIGATION_TIMEOUT')
        self.assertEqual(model.inputs, [])

    def test_config_rejects_invalid_deadlines(self):
        for value in (0, -1, 121, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                service(timeout=value)


class DraftApiTests(unittest.IsolatedAsyncioTestCase):
    def client(self, runner=None, **kwargs):
        runner = runner or service()[0]
        return httpx.AsyncClient(base_url='http://draft.test',
            transport=httpx.ASGITransport(app=create_app(service=runner, service_token=TOKEN, **kwargs)))

    async def test_auth_before_loading_body_or_dependencies(self):
        runner, reads, model = service()
        async with self.client(runner) as client:
            response = await client.post(PATH, content=b'private-chat'*10000)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('private-chat', response.text)
        self.assertEqual(reads.calls, []); self.assertEqual(model.inputs, [])

    async def test_body_size_and_safe_validation(self):
        runner, reads, _ = service()
        async with self.client(runner) as client:
            headers = {'Authorization': 'Bearer '+TOKEN}
            for data, status in ((b'private-chat'*10000, 413), (b'{"private-chat":1}', 422)):
                result = await client.post(PATH, content=data, headers=headers)
                self.assertEqual(result.status_code, status)
                self.assertNotIn('private-chat', result.text)
        self.assertEqual(reads.calls, [])

    async def test_successful_response_is_draft_only(self):
        async with self.client() as client:
            response = await client.post(PATH, json=payload(), headers={'Authorization': 'Bearer '+TOKEN})
        self.assertEqual(response.status_code, 200)
        result = DraftResponse.model_validate(response.json())
        self.assertFalse(result.execution_authorized)
        self.assertFalse(result.draft.execution_authorized)

    async def test_stale_is_conflict_not_success(self):
        f = deepcopy(FIXTURE); f['case']['evidenceVersion'] += 1
        async with self.client(service(fixture=f)[0]) as client:
            result = await client.post(PATH, json=payload(), headers={'Authorization': 'Bearer '+TOKEN})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['error']['code'], 'STALE_EVIDENCE')
        self.assertNotIn('draft', result.json())

    async def test_backpressure_and_release_after_failure(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Slow:
            async def run(self, request):
                started.set(); await release.wait()
                raise RuntimeError('private-failure')
        async with self.client(Slow()) as client:
            kwargs = {'json': payload(), 'headers': {'Authorization': 'Bearer '+TOKEN}}
            first = asyncio.create_task(client.post(PATH, **kwargs))
            await started.wait()
            busy = await client.post(PATH, **kwargs)
            self.assertEqual(busy.status_code, 429)
            release.set()
            for response in (await first, await client.post(PATH, **kwargs)):
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('private-failure', response.text)

    async def test_known_errors_return_only_stable_codes(self):
        class Broken:
            async def run(self, request):
                raise AgentToolError(self.code, trace_id='private-trace')
        broken = Broken()
        async with self.client(broken) as client:
            for code, status in [('INVESTIGATION_TIMEOUT', 504), ('EXECUTION_LIMIT', 503),
                                 ('INVALID_MODEL_OUTPUT', 502), ('POLICY_MISMATCH', 409),
                                 ('private-upstream-error', 503)]:
                broken.code = code
                response = await client.post(PATH, json=payload(), headers={'Authorization': 'Bearer '+TOKEN})
                self.assertEqual(response.status_code, status)
                self.assertNotIn('private-', response.text)

    async def test_default_disabled_and_missing_auth_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            app = configured_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://draft.test') as client:
            self.assertFalse((await client.get('/health')).json()['configured'])
            self.assertEqual((await client.post(PATH, content=b'bad')).status_code, 503)
        app = create_app(service_token=TOKEN)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://draft.test') as client:
            response = await client.post(PATH, json=payload(), headers={'Authorization':'Bearer '+TOKEN})
            self.assertEqual(response.json()['error']['code'], 'INVESTIGATION_NOT_CONFIGURED')

    @unittest.skipUnless(os.getenv('LIVEGUARD_TEST_TCP') == '1', 'TCP opt-in')
    async def test_real_tcp_draft_readonly_orchestration(self):
        runner, reads, model = service()
        with tcp_app(create_app(service=runner, service_token=TOKEN)) as url:
            async with httpx.AsyncClient(base_url=url) as client:
                self.assertEqual((await client.post(PATH, json=payload())).status_code, 401)
                response = await client.post(PATH, json=payload(), headers={'Authorization':'Bearer '+TOKEN})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()['executionAuthorized'])
        self.assertEqual(reads.calls, ['get_moderation_case', 'get_chat_context'])
        self.assertEqual(len(model.inputs), 1)
