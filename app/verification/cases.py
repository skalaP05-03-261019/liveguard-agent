"""Frozen synthetic cases. Expected answers never enter model-visible tool data."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from app.business_mcp.schemas import BackendError


def uid(value):
    return str(uuid5(NAMESPACE_URL, 'liveguard-verification/' + value))


def make_fixture(case):
    ident = case['id']
    t = datetime(2026, 10, 20, 3, tzinfo=timezone.utc)
    target_id = uid(ident + '/target')
    messages = []
    for i, item in enumerate(case.get('context', [])):
        messages.append({
            'messageId': uid(f'{ident}/context/{i}'), 'authorAlias': item.get('authorAlias', '가상시청자B'),
            'text': item['text'], 'sentAt': (t - timedelta(seconds=4-i)).isoformat(),
            'visibility': 'PUBLIC', 'replyToId': None,
        })
    messages.append({'messageId': target_id, 'authorAlias': '가상시청자A', 'text': case['text'],
                     'sentAt': t.isoformat(), 'visibility': 'HELD', 'replyToId': None})
    task = {'eventId': uid(ident+'/event'), 'taskId': uid(ident+'/task'), 'caseId': uid(ident+'/case'),
            'kind': 'ANALYZE', 'threadId': f'verify:{ident}', 'snapshotId': uid(ident+'/snapshot'),
            'expectedEvidenceVersion': 1, 'decisionId': None, 'traceId': uid(ident+'/trace')}
    snapshot = {'caseId': task['caseId'], 'snapshotId': task['snapshotId'], 'evidenceVersion': 1,
                'policyVersion': '3.1', 'targetMessageIds': [target_id], 'targetVersions': {target_id: 1}}
    context = {'caseId': task['caseId'], 'snapshotId': task['snapshotId'],
               'evidenceVersion': 1, 'messages': messages}
    return {'task': task, 'case': snapshot, 'context': context}


class SyntheticBackend:
    """An explicit test dependency behind the production Business MCP. Never Spring DB."""

    def __init__(self, cases):
        self.by_case = {make_fixture(c)['task']['caseId']: (c, make_fixture(c)) for c in cases}
        self.calls = []
        self.mutations = 0

    async def get_case_context(self, case_id, *, view, trace_id, params=None):
        entry = self.by_case.get(str(case_id))
        if entry is None:
            raise BackendError('NOT_FOUND', 'Synthetic case not found', retryable=False, trace_id=trace_id)
        case, fixture = entry
        self.calls.append({'view': view, 'traceId': trace_id})
        if view == 'moderationCase': return deepcopy(fixture['case'])
        if view == 'chatContext': return deepcopy(fixture['context'])
        defaults = {
            'streamInfo': {'liveId': uid('live'), 'title': '가상 검증 방송',
                           'notice': '가상시청자A/B는 일반 시청자이며 운영자가 아닙니다.'},
            'officialLinks': {'domains': ['official.example.test']},
            'securityFacts': {'piiExists': 'UNKNOWN', 'consent': 'UNKNOWN',
                              'isPublicOrOfficial': 'UNKNOWN', 'sourceId': None,
                              'notice': '실제 개인정보 진위를 판별하지 않는 합성 fixture입니다.'},
        }
        return deepcopy(case.get('facts', {}).get(view, defaults.get(view, {})))

    async def get_user_history(self, case_id, *, days, trace_id):
        self.calls.append({'view': 'history', 'traceId': trace_id})
        return {'confirmedCount': 0, 'days': days, 'note': '가상 검증 이력. 강퇴 집계 아님.'}

    async def execute_action(self, *args, **kwargs):
        self.mutations += 1
        raise RuntimeError('Verification forbids mutations')

    async def reconcile_review(self, *args, **kwargs):
        self.mutations += 1
        raise RuntimeError('Verification forbids mutations')
