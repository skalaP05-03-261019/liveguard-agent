"""Schemas, a controlled end-to-end handoff example, and opt-in read-only MCP inspection."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from pydantic import ValidationError
from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.schemas import TaskEnvelope
from .contracts import ScreenRequest, ScreenResult, InvestigationDraft, InvestigationHandoff
from .handoff import prepare_handoff, run_readonly_investigation
from .service import DraftRequest, DraftResponse, service_from_env


def summary(handoff):
    return {'contractVersion': handoff.contract_version, 'caseId': str(handoff.task.case_id),
            'liveId': str(handoff.snapshot.live_id), 'snapshotId': str(handoff.snapshot.snapshot_id),
            'evidenceVersion': handoff.snapshot.evidence_version, 'policyVersion': handoff.snapshot.policy_version,
            'targetCount': len(handoff.snapshot.target_message_ids), 'executionAuthorized': False}


async def example(path):
    from copy import deepcopy
    data = json.loads(path.read_text())
    calls = []

    class FixtureReads:
        async def call(self, name, arguments):
            calls.append(name)
            key = {'get_moderation_case': 'case', 'get_chat_context': 'context'}[name]
            return {'data': deepcopy(data[key]), 'traceId': arguments['traceId']}

    class FixtureInvestigator:
        async def investigate(self, request):
            return InvestigationDraft.model_validate(data['draft'])

    handoff = await prepare_handoff(task=TaskEnvelope.model_validate(data['task']),
                                    request=ScreenRequest.model_validate(data['screenRequest']),
                                    result=ScreenResult.model_validate(data['screenResult']), business=FixtureReads())
    draft = await run_readonly_investigation(handoff, FixtureInvestigator())
    return {**summary(handoff), 'kind': 'controlled-fixture', 'readTools': calls,
            'verdicts': [f.verdict.value for f in draft.analysis.message_findings],
            'humanDecisionRequired': True, 'backendSubmissionExecuted': False,
            'limits': ['Fixture BE and investigator; no real model, task acceptance, hiding or sanction.']}


async def inspect(args):
    handoff = await prepare_handoff(task=TaskEnvelope.model_validate_json(args.task.read_text()),
        request=ScreenRequest.model_validate_json(args.request.read_text()),
        result=ScreenResult.model_validate_json(args.result.read_text()),
        business=BusinessMcpReadClient(BusinessMcpSettings.from_env()))
    return {**summary(handoff), 'kind': 'observed-read-only', 'status': 'HANDOFF_READY',
            'backendSubmissionExecuted': False}


async def draft(args):
    response = await service_from_env().run(DraftRequest.model_validate_json(args.input.read_text()))
    if args.output:
        # Exclusive creation, private permissions; do not overwrite previous/private results.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(response.model_dump_json(by_alias=True, indent=2)+'\n')
    return {'kind': 'observed-read-only-draft', 'taskId': str(response.task_id),
            'requestId': str(response.request_id), 'traceId': str(response.trace_id),
            'caseId': str(response.draft.analysis.case_id),
            'policyVersion': response.draft.analysis.policy_version,
            'verdicts': [f.verdict.value for f in response.draft.analysis.message_findings],
            'executionAuthorized': False, 'backendSubmissionExecuted': False,
            'outputWritten': args.output is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    schemas = sub.add_parser('schemas'); schemas.add_argument('--out-dir', type=Path, required=True)
    run = sub.add_parser('example'); run.add_argument('--fixture', type=Path, required=True)
    check = sub.add_parser('inspect')
    for name in ('task', 'request', 'result'):
        check.add_argument('--'+name, type=Path, required=True)
    analyze = sub.add_parser('draft')
    analyze.add_argument('--input', type=Path, required=True)
    analyze.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'schemas':
            args.out_dir.mkdir(parents=True, exist_ok=True)
            models = (ScreenRequest, ScreenResult, InvestigationHandoff, InvestigationDraft,
                      DraftRequest, DraftResponse)
            paths = [args.out_dir/(m.__name__+'.json') for m in models]
            if any(p.exists() for p in paths):
                raise ValueError('Schema files already exist')
            for model, path in zip(models, paths):
                path.write_text(json.dumps(model.model_json_schema(by_alias=True), ensure_ascii=False, indent=2)+'\n')
            result = {'kind': 'schema-export', 'contracts': [m.__name__ for m in models]}
        elif args.command == 'example':
            result = asyncio.run(example(args.fixture))
        elif args.command == 'draft':
            result = asyncio.run(draft(args))
        else:
            result = asyncio.run(inspect(args))
    except AgentToolError as error:
        # Agent-owned stable error code only; never print response/input/exception text.
        print(json.dumps({'status': 'BLOCKED', 'code': error.code}))
        return 1
    except (ValueError, ValidationError, OSError, KeyError, TypeError):
        print(json.dumps({'status': 'BLOCKED', 'code': 'INVALID_CONFIGURATION_OR_FIXTURE'}))
        return 2
    except Exception:
        print(json.dumps({'status': 'BLOCKED', 'code': 'INVESTIGATION_DEPENDENCY_UNAVAILABLE'}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


raise SystemExit(main())
