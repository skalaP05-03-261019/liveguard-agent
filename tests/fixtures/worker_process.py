"""Controlled model/BE gateway in a separate OS process; real Inbox/checkpoints only.

Only tests invoke this fixture. It never connects to an actual business service.
"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path.cwd())]
logging.disable(logging.CRITICAL)

from app.harness.checkpoint import open_checkpointer
from app.integration.workflow import build_moderation_graph
from app.runtime.fencing import FencedPostgresSaver
from app.runtime.inbox import PostgresInbox
from app.runtime.worker import GraphWorker, WorkerSettings
from test_investigation import Policies
from test_workflow_integration import WorkflowTests, REVIEW, DECISION, KICK, USER


async def run(args):
    fixture = WorkflowTests()
    await fixture.prepare()
    backend = fixture.backend
    backend.decision = args.decision
    backend.require_kick = args.decision == 'CONFIRM_VIOLATION'

    def record(event, value=None):
        with args.audit.open('a') as stream:
            stream.write(json.dumps({'event': event, 'value': value})+'\n')

    original_create = backend.create_review
    original_apply = backend.apply_context
    original_get_decision = backend.get_decision
    original_model = fixture.model.complete

    async def create(**kwargs):
        record('create', kwargs['card'])
        if args.block_create:
            await asyncio.Event().wait()
        return await original_create(**kwargs)

    async def apply(**kwargs):
        record('apply', kwargs['request'])
        return await original_apply(**kwargs)

    async def model(*a, **kw):
        record('model')
        return await original_model(*a, **kw)

    async def get_decision(**kwargs):
        result = await original_get_decision(**kwargs)
        if kwargs['review_id'] == KICK:
            result['decision'] = 'REJECT' if args.kick_status == 'REJECTED' else 'APPROVE'
        return result

    async def revalidate(**kwargs):
        record('revalidate', kwargs)
        result = {**backend.refs(), 'reviewId': kwargs['review_id'],
                  'decisionId': kwargs['decision_id'], 'status': args.kick_status}
        if args.kick_status == 'APPROVED':
            result['actionCommand'] = {
                'actionId': DECISION, 'planId': REVIEW, 'type': 'KICK',
                'caseId': fixture.state['case_id'], 'targetId': USER,
                'decisionId': kwargs['decision_id'], 'expectedEvidenceVersion': 1,
                'expectedTargetVersion': 1, 'idempotencyKey': DECISION,
                'violationIds': [REVIEW, DECISION, fixture.state['case_id']],
                'policyVersion': fixture.state['policy_version']}
        return result

    async def kick(command, **kwargs):
        record('kick', command)
        return {'actionId': command['actionId'], 'status': 'APPLIED'}

    async def read_case(**kwargs):
        record('read_case', kwargs)
        return backend.refs()

    async def read_action(**kwargs):
        record('read_action', kwargs)
        return {'actionId': kwargs['action_id'], 'status': 'APPLIED'}

    backend.create_review, backend.apply_context = create, apply
    backend.get_decision = get_decision
    backend.revalidate, backend.kick = revalidate, kick
    backend.read_case, backend.read_action = read_case, read_action
    fixture.model.complete = model
    url = os.environ['AGENT_TEST_CHECKPOINT_DB_URL']
    store = PostgresInbox(url, schema=args.schema)
    async with open_checkpointer(url, schema=args.checkpoint_schema,
                                 saver_type=FencedPostgresSaver) as saver:
        graph = build_moderation_graph(model=fixture.model, business=fixture.business,
            policy=Policies(), backend=backend, checkpointer=saver, observer=lambda *_: None)
        worker = GraphWorker(store, graph, gateway=backend,
            settings=WorkerSettings(worker_id='process-'+str(os.getpid()), lease_seconds=3,
                                    heartbeat_seconds=.2))
        await worker.run_one()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--schema', required=True)
    parser.add_argument('--checkpoint-schema', required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--decision', default='CONFIRM_VIOLATION')
    parser.add_argument('--kick-status', default='APPROVED')
    parser.add_argument('--block-create', action='store_true')
    try:
        asyncio.run(run(parser.parse_args()))
    except Exception as error:
        print(json.dumps({'status': 'BLOCKED', 'errorType': type(error).__name__}))
        raise SystemExit(1)
