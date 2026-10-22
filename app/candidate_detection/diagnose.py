"""Development-only observations against AI drafts, never service ground-truth metrics."""

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from uuid import NAMESPACE_URL, uuid5

from app.clients.business_mcp import AgentToolError
from app.investigation.model import model_from_env
from .contracts import Assessment, EXPERIMENTS, ScreeningInput, select_candidate, validate_evidence
from .curation import ReviewSet, bind_source, write_new
from .screen import CandidateScreen, PROMPT


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def development_inputs(review, source):
    # Check purpose before binding or constructing any model inputs.
    if review.purpose != 'development' or review.source_split != 'train':
        raise ValueError('Confirmation inputs cannot enter development diagnostics')
    bind_source(review, source)
    return [(item, ScreeningInput(liveId=uuid5(NAMESPACE_URL, 'unsmile:'+review.source_revision),
                target={'messageId': uuid5(NAMESPACE_URL, item.id), 'text': item.text}, context=[]))
            for item in review.items]


async def diagnose(review, *, source, screen=None, predictions=None, limit=None):
    if (screen is None) == (predictions is None):
        raise ValueError('Choose live screen or offline observations')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 100):
        raise ValueError('Limit must be 1..100')
    if screen is not None and limit is None:
        raise ValueError('Live diagnostics need an explicit limit')
    inputs = development_inputs(review, source)
    if limit is not None:
        inputs = inputs[:limit]
    if predictions is not None and (not isinstance(predictions, dict)
            or set(predictions) != {item.id for item, _ in inputs}):
        raise ValueError('Offline observation IDs must match selected inputs')
    results, blocked = [], False
    for item, data in inputs:
        row = {'id': item.id, 'reviewStatus': item.status}
        if blocked:
            results.append({**row, 'status': 'NOT_RUN', 'errorCode': 'MODEL_AUTH_BLOCKED'})
            continue
        started = time.monotonic()
        try:
            if screen is not None:
                # Only original text/IDs, never source labels, drafts or review labels.
                assessment, usage = await screen.assess(data, trace_id=str(uuid5(NAMESPACE_URL, item.id)))
            else:
                assessment = validate_evidence(data, Assessment.model_validate(predictions[item.id]))
                usage = {}
            candidates = {name: select_candidate(assessment, name) for name in EXPERIMENTS}
            draft = item.proposal.expected_candidate if item.proposal else None
            comparison = {name: ('UNRESOLVED' if draft is None else
                                'AGREES_WITH_AI_DRAFT' if candidate == draft else 'DIFFERS_FROM_AI_DRAFT')
                          for name, candidate in candidates.items()}
            row.update(status='ASSESSED', candidates=candidates, draftComparison=comparison,
                       assessment=assessment.model_dump(mode='json', by_alias=True), usage=usage)
        except Exception as error:
            code = error.code if isinstance(error, AgentToolError) else 'DETECTOR_ERROR'
            code = code if re.fullmatch(r'[A-Z_]{1,64}', str(code)) else 'DETECTOR_ERROR'
            row.update(status='ERROR', errorCode=code)
            blocked = code in {'UNAUTHENTICATED', 'FORBIDDEN'}
        row['latencyMs'] = round((time.monotonic()-started)*1000, 2)
        results.append(row)
    assessed = [r for r in results if r['status'] == 'ASSESSED']
    latencies = sorted(r['latencyMs'] for r in results if 'latencyMs' in r)
    percentile = lambda p: latencies[max(0, math.ceil(len(latencies)*p)-1)] if latencies else None
    summary = {
        'kind': 'development-ai-draft-diagnostic-only',
        'sourceRevision': review.source_revision, 'sourceSha256': review.source_sha256,
        'reviewSha256': digest(review.model_dump(mode='json', by_alias=True)),
        'inputSha256': digest([data.model_dump(mode='json', by_alias=True) for _, data in inputs]),
        'total': len(results), 'assessed': len(assessed),
        'errors': sum(r['status'] == 'ERROR' for r in results),
        'notRun': sum(r['status'] == 'NOT_RUN' for r in results),
        'reviewStatuses': dict(Counter(r['reviewStatus'] for r in results)),
        'candidateCounts': {name: sum(r['candidates'][name] for r in assessed) for name in EXPERIMENTS},
        'aiDraftComparison': {name: dict(Counter(r['draftComparison'][name] for r in assessed))
                              for name in EXPERIMENTS},
        'errorCodes': dict(Counter(r['errorCode'] for r in results if r['status'] == 'ERROR')),
        'latencyMs': {'p50': percentile(.5), 'p95': percentile(.95)},
        'usage': {key: sum(r.get('usage', {}).get(key, 0) for r in assessed)
                  for key in ('inputTokens', 'outputTokens')},
        'usesHumanGroundTruthForScoring': False, 'serviceMetricsComputed': False,
        'enforcementAuthorized': False,
        'limits': ['AI-draft agreement is not accuracy, precision, recall or policy approval.',
                   'Source labels and all review/draft fields are withheld from the detector.',
                   'Development only; no confirmation inputs or prompt modifications.',
                   'No Backend/FE delivery, enforcement or production throughput tested.',
                   'Latency is sequential model-call observation, not streaming E2E latency.'],
    }
    return {'summary': summary, 'results': results}


def review_queue(review, report):
    """Private plain text: never treat dataset/model text as executable markup."""
    by_id = {item.id: item for item in review.items}
    def rank(row):
        if row['status'] != 'ASSESSED': return 0
        comparison = row['draftComparison']['clear-or-possible']
        return {'DIFFERS_FROM_AI_DRAFT': 1, 'UNRESOLVED': 2, 'AGREES_WITH_AI_DRAFT': 3}[comparison]
    lines = ['개발용 검수 참고 큐 — 자동 승인 아님 / 서비스 성능 측정 아님',
             '정책·QA 담당이 모델 출력·AI 초안 모두 수정할 수 있습니다.',
             '순서: 호출 오류 → 중간 선택 기준 불일치 → 초안 미정 → 일치.',
             '모든 원문·AI/모델 분석은 로컬 보관. 확인용 자료는 포함하지 않습니다.', '']
    for row in sorted(report['results'], key=rank):
        item = by_id[row['id']]
        lines += [f"ID: {item.id} / 검수 상태: {item.status}", f'원문: {item.text}',
                  '원본 라벨(정답 아님): '+json.dumps(item.original_labels, ensure_ascii=False),
                  'AI 초안(미승인): '+(item.proposal.model_dump_json(by_alias=True) if item.proposal else '없음'),
                  '모델 관찰(미승인): '+json.dumps(row, ensure_ascii=False), '']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true', help='Opt in to configured model/API usage')
    mode.add_argument('--predictions', type=Path)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--timeout', type=float, default=8)
    parser.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.out_dir.exists(): raise ValueError('Use a new output directory')
        review = ReviewSet.model_validate_json(args.review.read_text())
        # Fail before model initialization and before exposing any confirmation text.
        development_inputs(review, args.source)
        if args.live:
            if args.limit is None: raise ValueError('Explicit live limit required')
            screen = CandidateScreen(model_from_env(), timeout_seconds=args.timeout)
            report = asyncio.run(diagnose(review, source=args.source, screen=screen, limit=args.limit))
            execution = {'mode': 'live-model', 'sourceModel': os.getenv('MODEL_NAME'),
                         'promptSha256': hashlib.sha256(PROMPT.read_bytes()).hexdigest(),
                         'timeoutSeconds': args.timeout}
        else:
            predictions = json.loads(args.predictions.read_text())
            report = asyncio.run(diagnose(review, source=args.source, predictions=predictions, limit=args.limit))
            execution = {'mode': 'offline-observations', 'promptExecuted': False}
        report['summary']['execution'] = execution
        args.out_dir.mkdir(parents=True, mode=0o700)
        write_new(args.out_dir/'diagnostics.json', report)
        write_new(args.out_dir/'summary.json', report['summary'])
        fd = os.open(args.out_dir/'review-queue.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream: stream.write(review_queue(review, report))
    except (ValueError, OSError, TypeError, KeyError):
        parser.exit(2, 'Invalid private source/review/configuration; no exception text emitted.\n')
    summary = report['summary']
    print(json.dumps({key: summary[key] for key in ('kind', 'total', 'assessed', 'errors', 'notRun')}))
    return 1 if summary['errors'] or summary['notRun'] else 0


if __name__ == '__main__': raise SystemExit(main())
