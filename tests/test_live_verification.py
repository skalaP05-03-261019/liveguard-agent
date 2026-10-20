"""Offline regression for paid verification runner; never calls OpenAI in CI."""

from argparse import Namespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.investigation.model import model_from_env
from app.verification.cases import SyntheticBackend, make_fixture
from app.verification.run import (FIXTURES, MODEL, assess, check_db_scope, load_cases,
                                   policy_chunks, rule_dispatch, verify, VerificationBlocked)


class LiveSuiteTests(unittest.TestCase):
    def test_frozen_suite_and_policy(self):
        cases = load_cases()
        self.assertEqual(len(cases), 15)
        self.assertEqual(sum(c.get('smoke',False) for c in cases), 3)
        chunks = policy_chunks()
        self.assertEqual(len(chunks),29)
        by_id = {c.policy_id:c for c in chunks}
        self.assertEqual(by_id['CP-5.2'].category, 'PHISHING')
        self.assertEqual(by_id['CP-4.2'].category, 'FLOOD')
        self.assertTrue(all(c.version=='3.1' for c in chunks))

    def test_answers_not_exposed_by_fixture(self):
        for case in load_cases():
            fixture = make_fixture(case)
            self.assertNotIn('\"expected\":', json.dumps(fixture))
            self.assertEqual(fixture['case']['policyVersion'],'3.1')
            self.assertEqual(fixture['case']['targetMessageIds'],[fixture['context']['messages'][-1]['messageId']])

    def test_only_explicit_rule_cases_skip_model(self):
        rules = [c for c in load_cases() if rule_dispatch(c)]
        self.assertEqual(len(rules),2)
        self.assertTrue(all(assess(c['expected'],rule_dispatch(c)) for c in rules))
        self.assertEqual(rule_dispatch(rules[1])['policyIds'], ['CP-5.1'])
        self.assertTrue(rules[1]['ruleSignals']['isOfficial'])

    def test_assessment_checks_verdict_category_and_policy(self):
        expected = {'verdict':'VIOLATION','categories':['HARASSMENT'],'policyAnyOf':['CP-3.2']}
        good = {'verdict':'VIOLATION','categories':['HARASSMENT'],'policyIds':['CP-3.2']}
        self.assertTrue(assess(expected,good))
        for field,value in [('verdict','NO_VIOLATION'),('categories',[]),('policyIds',['CP-5.1'])]:
            self.assertFalse(assess(expected,{**good,field:value}))

    def test_db_guard_blocks_shared_database(self):
        for name in ['postgres','rag','platform','liveguard']:
            with self.assertRaises(ValueError): check_db_scope(f'postgresql://u:p@localhost/{name}')
        check_db_scope('postgresql://u:p@localhost/liveguard_eval')

    def test_key_fallback_and_explicit_override(self):
        base = {'MODEL_PROVIDER':'openai','MODEL_NAME':'gpt-5.4-mini','OPENAI_API_KEY':'test-standard'}
        with patch.dict('os.environ',base,clear=True):
            self.assertEqual(model_from_env()._api_key,'test-standard')
        with patch.dict('os.environ',{**base,'MODEL_API_KEY':'test-override'},clear=True):
            self.assertEqual(model_from_env()._api_key,'test-override')
        with patch.dict('os.environ',{**base,'MODEL_API_KEY':''},clear=True):
            self.assertEqual(model_from_env()._api_key,'test-standard')


class LiveRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocked_preflight_writes_honest_no_model_report(self):
        async def blocked(db,key):
            raise VerificationBlocked('database','DATABASE_UNREACHABLE')
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ',{'OPENAI_API_KEY':'not-real'}):
            args = Namespace(mode='smoke', db_url='postgresql://unused/liveguard_eval',
                             output=str(Path(folder)/'report.json'), smoke_report=None)
            result = await verify(args,check=blocked)
            saved = json.loads(Path(args.output).read_text())
            self.assertEqual(result['status'],'BLOCKED')
            self.assertEqual(saved['results'],[])
            self.assertFalse(saved['liveModelExecuted'])
            self.assertFalse(saved['realIndexBuilt'])
            self.assertNotIn('not-real',Path(args.output).read_text())

    async def test_full_requires_matching_pass_smoke(self):
        async def unexpected(*args):
            self.fail('Preflight must not run before smoke gate')
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ',{'OPENAI_API_KEY':'not-real'}):
            args = Namespace(mode='full', db_url='postgresql://unused/liveguard_eval',
                             output=str(Path(folder)/'report.json'), smoke_report=None)
            result = await verify(args,check=unexpected)
            self.assertEqual(result['errorCode'],'PASSING_SMOKE_REPORT_REQUIRED')

    async def test_synthetic_backend_never_returns_expected_answers(self):
        cases = load_cases()
        backend = SyntheticBackend(cases)
        for case in cases:
            fixture = make_fixture(case)
            result = await backend.get_case_context(fixture['task']['caseId'],view='securityFacts',trace_id='test')
            self.assertNotIn('expected',result)
        with self.assertRaises(RuntimeError): await backend.execute_action({})
        self.assertEqual(backend.mutations,1)

    async def test_complete_smoke_orchestration_with_explicit_dependency_doubles(self):
        """Runner plumbing only: model, embedder and storage doubled; real MCP apps/SDK."""
        from contextlib import ExitStack
        from unittest.mock import MagicMock
        from app.investigation.model import ModelTurn, ToolCall
        from app.rag_mcp.config import EMBEDDING_DIMENSIONS
        chunks = {c.policy_id:c for c in policy_chunks()}
        class Repository:
            preferred = 'CP-2.2'
            async def list_versions(self): return {'3.1':'text-embedding-3-small'}
            async def list_policy_ids(self,version): return list(chunks)
            async def search(self,vector,**kwargs):
                return [{**await self.get_section(self.preferred,'3.1'),'score':0.9}]
            async def get_section(self,pid,version):
                c=chunks[pid]
                return {'policy_id':pid,'version':'3.1','title':c.title,'text':c.text,
                        'category':c.category.value if c.category else None,'resource_uri':c.resource_uri}
        repository = Repository()
        class Embedder:
            model='text-embedding-3-small'
            def embed(self,texts): return [[0.0]*EMBEDDING_DIMENSIONS for _ in texts]
            async def embed_query(self,text):
                repository.preferred=text
                return [0.0]*EMBEDDING_DIMENSIONS
        selected=[c for c in load_cases() if c.get('smoke')]
        class Model:
            calls=0
            async def complete(self,messages,tools,*,trace_id):
                case=selected[self.calls//2]
                self.calls+=1
                pid={'normal-praise':'CP-2.2','violation-harassment':'CP-3.2',
                     'review-pii-unknown':'SP-3.1'}[case['id']]
                if self.calls%2:
                    return ModelTurn([ToolCall(str(self.calls),'search_policy',{'query':pid,'topK':1})])
                fixture=make_fixture(case)
                return ModelTurn([ToolCall(str(self.calls),'finish_investigation',{'messageFindings':[{
                    'messageId':fixture['case']['targetMessageIds'][0],
                    'verdict':case['expected']['verdict'],'categories':case['expected']['categories'],
                    'policyIds':[pid],'evidenceMessageIds':fixture['case']['targetMessageIds'],
                    'reason':'orchestration fixture, not actual model quality',
                    'missingFacts':['fixture unavailable fact'] if case['expected']['verdict']=='NEEDS_REVIEW' else []
                }]})],{'inputTokens':10,'outputTokens':2})
        async def ready(*args): pass
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ',{'OPENAI_API_KEY':'never-real'}))
            stack.enter_context(patch('app.verification.run.OpenAIEmbedder',return_value=Embedder()))
            stack.enter_context(patch('app.rag_mcp.main.AsyncOpenAIEmbedder',return_value=Embedder()))
            stack.enter_context(patch('app.rag_mcp.main.PgPolicyRepository',return_value=repository))
            stack.enter_context(patch('app.verification.run.OpenAIInvestigationModel',return_value=Model()))
            stack.enter_context(patch('app.verification.run.psycopg.connect',return_value=MagicMock()))
            for function in ('ensure_schema','replace_version'):
                stack.enter_context(patch('app.verification.run.store.'+function))
            stack.enter_context(patch('app.verification.run.store.count_version',return_value=29))
            args=Namespace(mode='smoke',db_url='postgresql://unused/liveguard_eval',
                           output=str(Path(folder)/'report.json'),smoke_report=None)
            result=await verify(args,check=ready)
            self.assertEqual(result['status'],'PASS',result['results'])
            self.assertEqual(len(result['results']),3)
            self.assertEqual(result['mutations'],0)
            self.assertTrue(all(r['modelCalls']==2 for r in result['results']))
            self.assertNotIn('never-real',Path(args.output).read_text())
