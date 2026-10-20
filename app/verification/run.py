"""Opt-in paid verification: real OpenAI/embeddings/SQL/MCP, synthetic business data.

Run with uv run --env-file ~/Downloads/.env python -m app.verification.run ...
No fallback to fake embeddings, policies, model output or actual Spring Backend.
"""

import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import time

import httpx2
import psycopg
from openai import AsyncOpenAI

from app.business_mcp.config import Settings as BusinessSettings
from app.business_mcp.main import create_app as business_app
from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.investigation.graph import build_investigation_graph
from app.investigation.model import OpenAIInvestigationModel
from app.investigation.runner import PROMPT
from app.harness.safety import safe_presentation
from app.rag_mcp.config import ServerSettings
from app.rag_mcp.embeddings import OpenAIEmbedder
from app.rag_mcp.policy import parse_policy
from app.rag_mcp.main import create_app as rag_app
from app.rag_mcp import store
from app.schemas import Category, TaskEnvelope
from app.verification.cases import SyntheticBackend, make_fixture


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests' / 'fixtures' / 'live-verification'
POLICY_COMMIT = 'fd3988fffcc85d5a56cdfdd9054377dc284ecf85'
# #16's v3.1 additions, applied only to verification chunks, not provider code.
V31_CATEGORIES = {'CP-4.2': Category.FLOOD, 'CP-5.2': Category.PHISHING}
MODEL = 'gpt-5.4-mini'
EMBEDDING_MODEL = 'text-embedding-3-small'


class VerificationBlocked(Exception):
    def __init__(self, stage, code):
        self.stage, self.code = stage, code


def load_cases(path=FIXTURES / 'cases.json'):
    data = json.loads(Path(path).read_text())
    cases = data['cases']
    if data['policyVersion'] != '3.1' or len(cases) != 15:
        raise ValueError('Expected frozen v3.1 suite of 15 cases')
    if len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Duplicate verification case ID')
    if sum(c.get('smoke', False) for c in cases) != 3:
        raise ValueError('Expected exactly 3 smoke cases')
    return cases


def policy_chunks(path=FIXTURES / 'policy-v3.1.md'):
    chunks = [replace(c, category=V31_CATEGORIES.get(c.policy_id, c.category))
              for c in parse_policy(Path(path).read_text(), version='3.1')]
    if len(chunks) != 29:
        raise ValueError('Expected 29 v3.1 policy clauses')
    return chunks


def check_db_scope(url):
    # Require a dedicated database before CREATE/DELETE/INSERT. Do not print URL.
    info = psycopg.conninfo.conninfo_to_dict(url)
    if not info.get('dbname', '').startswith('liveguard_eval'):
        raise ValueError('Use a dedicated database named liveguard_eval*')


def rule_dispatch(case):
    """Verification dispatcher only. NOT a test/implementation of Backend filtering."""
    if case.get('path') != 'RULE_CONFIRMED':
        return None
    signals = case.get('ruleSignals', {})
    if signals.get('dictionaryMatches'):
        return {'verdict': 'VIOLATION', 'categories': ['PROFANITY'], 'policyIds': ['CP-2.1']}
    if signals.get('linkDetected') is True:
        return {'verdict': 'VIOLATION', 'categories': ['PHISHING'], 'policyIds': ['CP-5.1']}
    raise ValueError('Unrecognized verification rule signals')


def assess(expected, actual):
    policy_ids = set(actual['policyIds'])
    # A non-violation review may report suspected categories; only VIOLATION
    # categories are evaluated as asserted violations. No verdict forgiveness.
    return (actual['verdict'] == expected['verdict']
            and (actual['verdict'] == 'NEEDS_REVIEW'
                 or set(actual['categories']) == set(expected['categories']))
            and (not expected['policyAnyOf'] or bool(policy_ids & set(expected['policyAnyOf']))))


def write_report(path, report):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


async def preflight(db_url, key):
    try:
        check_db_scope(db_url)
    except (ValueError, psycopg.Error):
        raise VerificationBlocked('configuration', 'INVALID_VERIFICATION_DATABASE') from None
    try:
        async with await psycopg.AsyncConnection.connect(db_url, connect_timeout=3) as conn:
            cursor = await conn.execute('SELECT 1')
            await cursor.fetchone()
    except psycopg.Error:
        raise VerificationBlocked('database', 'DATABASE_UNREACHABLE') from None
    try:
        async with AsyncOpenAI(api_key=key, max_retries=0, timeout=15,
                               base_url='https://api.openai.com/v1') as client:
            await client.models.retrieve(MODEL)
    except Exception as error:
        # Do not serialize exception strings/bodies: may include credential-bearing URLs.
        code = 'OPENAI_AUTH_FAILED' if getattr(error, 'status_code', None) in {401,403} else 'OPENAI_UNREACHABLE'
        raise VerificationBlocked('openai_preflight', code) from None


async def verify(args, *, check=preflight):
    cases = load_cases()
    selected = [c for c in cases if c.get('smoke')] if args.mode == 'smoke' else cases
    report = {
        'schemaVersion': 1, 'mode': args.mode, 'startedAt': datetime.now(timezone.utc).isoformat(),
        'status': 'RUNNING', 'model': MODEL, 'embeddingModel': EMBEDDING_MODEL, 'policyVersion': '3.1',
        'policySource': {'pr': 16, 'commit': POLICY_COMMIT,
                         'sha256': hashlib.sha256((FIXTURES / 'policy-v3.1.md').read_bytes()).hexdigest()},
        'suiteSha256': hashlib.sha256((FIXTURES / 'cases.json').read_bytes()).hexdigest(),
        'promptSha256': hashlib.sha256(PROMPT.read_bytes()).hexdigest(),
        'businessDependency': 'synthetic cases behind production Business MCP, not Spring Backend',
        'transport': 'production MCP apps and SDK via authenticated ASGI HTTP; SQL and OpenAI use network',
        'expectedCases': len(selected), 'results': [], 'mutations': 0,
        'liveModelExecuted': False, 'liveModelAttempted': False, 'realIndexBuilt': False,
        'notice': 'Controlled evaluation, not real-user safety or actual Backend filtering proof.',
    }
    write_report(args.output, report)
    key = os.getenv('MODEL_API_KEY') or os.getenv('OPENAI_API_KEY', '')
    embedding_key = os.getenv('OPENAI_API_KEY', '')
    try:
        if not key or not embedding_key or not args.db_url:
            raise VerificationBlocked('configuration', 'MISSING_VERIFICATION_SETTINGS')
        if args.mode == 'full':
            try:
                smoke = json.loads(Path(args.smoke_report).read_text())
                smoke_ids = {c['id'] for c in cases if c.get('smoke')}
                if (smoke.get('mode') != 'smoke' or smoke.get('status') != 'PASS'
                        or smoke.get('model') != MODEL or smoke.get('policySource') != report['policySource']
                        or smoke.get('suiteSha256') != report['suiteSha256']
                        or smoke.get('promptSha256') != report['promptSha256']
                        or not smoke.get('liveModelExecuted') or not smoke.get('realIndexBuilt')
                        or {r['id'] for r in smoke.get('results', []) if r.get('passed')} != smoke_ids):
                    raise ValueError
            except (TypeError, ValueError, OSError, KeyError):
                raise VerificationBlocked('smoke_gate', 'PASSING_SMOKE_REPORT_REQUIRED') from None
        await check(args.db_url, key)
        chunks = policy_chunks()
        try:
            embedder = OpenAIEmbedder(embedding_key, EMBEDDING_MODEL)
            vectors = await asyncio.to_thread(embedder.embed, [c.embedding_input for c in chunks])
            with psycopg.connect(args.db_url) as conn:
                store.ensure_schema(conn)
                store.replace_version(conn, chunks, vectors, embedding_model=EMBEDDING_MODEL)
                indexed = store.count_version(conn, '3.1')
            if indexed != len(chunks):
                raise ValueError('Index count mismatch')
            report['realIndexBuilt'], report['indexRows'] = True, indexed
        except Exception:
            raise VerificationBlocked('index', 'REAL_INDEX_FAILED') from None
        backend = SyntheticBackend(selected)
        token = secrets.token_urlsafe(32)
        apps = {
            'business': business_app(BusinessSettings('http://synthetic.invalid', 'unused-fixture', token,
                                                       '127.0.0.1', 8002), backend=backend),
            'rag': rag_app(ServerSettings(args.db_url, embedding_key, EMBEDDING_MODEL, token,
                                          '127.0.0.1', 8003)),
        }
        def client_factory(app):
            def create(*, headers):
                return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers)
            return create
        business = BusinessMcpReadClient(BusinessMcpSettings('http://127.0.0.1:8002/mcp', token),
                                         http_client_factory=client_factory(apps['business']))
        policy = RagMcpReadClient(RagMcpSettings('http://127.0.0.1:8003/mcp', token),
                                 http_client_factory=client_factory(apps['rag']))
        delegate = OpenAIInvestigationModel(model=MODEL, api_key=key)
        class ObservedModel:
            last_finish_shape = None

            async def complete(self, messages, tools, *, trace_id):
                report['liveModelAttempted'] = True
                turn = await delegate.complete(messages, tools, trace_id=trace_id)
                report['liveModelExecuted'] = True
                for call in turn.calls:
                    if call.name == 'finish_investigation':
                        values = call.arguments.get('messageFindings', [])
                        if isinstance(values,list):
                            self.last_finish_shape = [{'verdict': item.get('verdict') if item.get('verdict') in
                                {'VIOLATION','NO_VIOLATION','NEEDS_REVIEW'} else 'INVALID',
                                'missingFactsCount': len(item['missingFacts']) if isinstance(item.get('missingFacts'),list) else None}
                                for item in values if isinstance(item,dict)]
                return turn
        model = ObservedModel()
        async with AsyncExitStack() as stack:
            for app in apps.values():
                await stack.enter_async_context(app.router.lifespan_context(app))
            for case in selected:
                start = time.monotonic()
                model.last_finish_shape = None
                result = {'id': case['id'], 'expected': case['expected'], 'passed': False}
                try:
                    rule = rule_dispatch(case)
                    if rule:
                        result.update(actual=rule, path='verification_rule_dispatch', modelCalls=0,
                                      backendRuleVerified=False)
                    else:
                        fixture = make_fixture(case)
                        graph = build_investigation_graph(model=model, business=business, policy=policy,
                                                          present_data=safe_presentation)
                        state = await graph.ainvoke({'task': TaskEnvelope.model_validate(fixture['task'])})
                        finding = state['message_findings'][0]
                        result.update(actual={'verdict': finding.verdict, 'categories': finding.categories,
                                              'policyIds': finding.policy_ids},
                                      messageId=str(finding.message_id), traceId=fixture['task']['traceId'],
                                      assessmentReason=safe_presentation(finding.reason),
                                      modelCalls=state['turn_count'], path='live_model_investigation',
                                      policyEvidenceIds=[p['policyId'] for p in state['policy_evidence']],
                                      toolCalls=[o['toolName'] for o in state['tool_observations'] if 'toolName' in o],
                                      usage=[o['usage'] for o in state['tool_observations'] if 'usage' in o])
                    result['passed'] = assess(case['expected'], result['actual'])
                except AgentToolError as error:
                    result.update(errorCode=error.code, retryable=error.retryable)
                    if error.code == 'INVALID_MODEL_OUTPUT':
                        result['modelOutputShape'] = model.last_finish_shape
                except Exception as error:
                    result.update(errorCode='VERIFICATION_CASE_FAILED', exceptionType=type(error).__name__)
                result['elapsedSeconds'] = round(time.monotonic() - start, 3)
                report['results'].append(result)
                write_report(args.output, report)
                print(json.dumps({'caseId': case['id'], 'passed': result['passed'],
                                  'errorCode': result.get('errorCode')}, ensure_ascii=False), flush=True)
                # Full evaluation starts only after three separate smoke cases pass.
                # The CLI caller runs smoke then full; stop a failing smoke immediately.
                if args.mode == 'smoke' and not result['passed']:
                    break
        report['mutations'] = backend.mutations
        report['status'] = 'PASS' if (len(report['results']) == len(selected)
                                     and all(r['passed'] for r in report['results'])
                                     and backend.mutations == 0) else 'FAIL'
    except VerificationBlocked as error:
        report.update(status='BLOCKED', blockedStage=error.stage, errorCode=error.code)
    except Exception as error:
        report.update(status='FAIL', errorCode='VERIFICATION_RUN_FAILED', exceptionType=type(error).__name__)
    finally:
        report['finishedAt'] = datetime.now(timezone.utc).isoformat()
        write_report(args.output, report)
    print(json.dumps({'status': report['status'], 'output': str(args.output),
                      'executedCases': len(report['results'])}, ensure_ascii=False))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['smoke','full'], required=True)
    parser.add_argument('--db-url', default=os.getenv('RAG_VERIFY_DB_URL',''), help='Dedicated liveguard_eval* DB only')
    parser.add_argument('--output', required=True)
    parser.add_argument('--smoke-report', help='Matching PASS smoke JSON required for --mode full')
    args = parser.parse_args()
    # Third-party wire logs must never dump model requests/credential-bearing URLs.
    logging.basicConfig(level=logging.WARNING)
    for name in ('openai','httpx','httpx2','mcp','httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    report = asyncio.run(verify(args))
    raise SystemExit(0 if report['status'] == 'PASS' else (2 if report['status'] == 'BLOCKED' else 1))


if __name__ == '__main__':
    main()
