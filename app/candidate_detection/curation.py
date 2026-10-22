"""Local UnSmile sampling and reviewed candidate export; no model, policy or Backend calls."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal
import unicodedata
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, Field, StrictBool, model_validator
from app.schemas import Contract
from .contracts import RiskKind
from .evaluate import EvalCase, fingerprint, load_cases

SOURCE_URL = 'https://github.com/smilegate-ai/korean_unsmile_dataset'
LABELS = ('여성/가족', '남성', '성소수자', '인종/국적', '연령', '지역', '종교', '기타 혐오', '악플/욕설', 'clean', '개인지칭')
BUCKETS = ('지역', '여성/가족', '남성', '성소수자', '인종/국적', '연령', '종교', '기타 혐오', '악플/욕설', 'clean')
Binary = Annotated[int, Field(strict=True, ge=0, le=1)]
Sha = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


def normalized_hash(text):
    # Exact/normalized overlap only. This does NOT detect paraphrases or semantic families.
    normalized = ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


def read_source(path):
    path = Path(path)
    if path.stat().st_size > 50_000_000:
        raise ValueError('Source too large')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        if reader.fieldnames != ['문장', *LABELS]:
            raise ValueError('Unexpected source schema')
        rows = []
        for index, raw in enumerate(reader, 1):
            if index > 25_000 or set(raw) != {'문장', *LABELS}:
                raise ValueError('Invalid source row count/columns')
            text = raw['문장']
            if not isinstance(text, str) or not text.strip() or len(text) > 500:
                raise ValueError('Source text outside candidate bounds')
            if any(raw[key] not in {'0', '1'} for key in LABELS):
                raise ValueError('Labels must be binary')
            rows.append({'row': index, 'text': text, 'labels': {key: int(raw[key]) for key in LABELS},
                         'normalizedSha256': normalized_hash(text)})
    if not rows:
        raise ValueError('Empty source')
    return digest, rows


def source_flags(labels):
    active = sum(labels[key] for key in BUCKETS if key != 'clean')
    return (["CLEAN_OVERLAP"] if labels['clean'] and active else []) + (
        ["MULTI_LABEL"] if active > 1 else []) + (["NO_ACTIVE_LABEL"] if not active and not labels['clean'] else []) + (
        ["INDIVIDUAL_REFERENCE"] if labels['개인지칭'] else [])


class ReviewProposal(Contract):
    kind: Literal['AI_DRAFT'] = 'AI_DRAFT'
    expected_candidate: StrictBool | None = None
    expected_kinds: list[RiskKind] | None = None
    rationale: str = Field(min_length=1, max_length=1000, strict=True)

    @model_validator(mode='after')
    def consistent_draft(self):
        if (not self.rationale.strip() or (self.expected_candidate is False and self.expected_kinds)
                or (self.expected_kinds is not None and len(set(self.expected_kinds)) != len(self.expected_kinds))):
            raise ValueError('Inconsistent draft expectation')
        return self


class ReviewItem(Contract):
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    source_row: int = Field(ge=1, strict=True)
    text: str = Field(min_length=1, max_length=500, strict=True)
    original_labels: dict[str, Binary]
    normalized_sha256: Sha
    source_flags: list[str]
    group_id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    status: Literal['PENDING', 'APPROVED', 'EXCLUDED', 'DISAGREEMENT'] = 'PENDING'
    expected_candidate: StrictBool | None = None
    expected_kinds: list[RiskKind] | None = None
    reviewer: str | None = Field(default=None, max_length=100)
    reviewed_at: AwareDatetime | None = None
    rationale: str | None = Field(default=None, max_length=1000)
    proposal: ReviewProposal | None = None

    @model_validator(mode='after')
    def review_is_explicit(self):
        if self.expected_kinds is not None and len(set(self.expected_kinds)) != len(self.expected_kinds):
            raise ValueError('Duplicate expected kind')
        if self.expected_candidate is False and self.expected_kinds:
            raise ValueError('Normal candidate label cannot expect risk kinds')
        if self.status in {'APPROVED', 'EXCLUDED'}:
            if not (self.reviewer and self.reviewer.strip() and self.reviewed_at
                    and self.rationale and self.rationale.strip()):
                raise ValueError('Completed review needs attribution, date and rationale')
            if self.status == 'APPROVED' and self.expected_candidate is None:
                raise ValueError('Service candidate expectation required')
        return self


class ReviewSet(Contract):
    version: Literal['candidate-review-v1'] = 'candidate-review-v1'
    source_url: Literal['https://github.com/smilegate-ai/korean_unsmile_dataset'] = SOURCE_URL
    source_revision: str = Field(pattern=r'^[a-f0-9]{40}$')
    source_sha256: Sha
    source_split: Literal['train', 'valid']
    purpose: Literal['development', 'confirmation']
    items: list[ReviewItem] = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def split_and_identity(self):
        if (self.source_split, self.purpose) not in {('train', 'development'), ('valid', 'confirmation')}:
            raise ValueError('Keep train/development and valid/confirmation separate')
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError('Duplicate case IDs')
        if len({item.source_row for item in self.items}) != len(self.items):
            raise ValueError('Duplicate source rows')
        return self


def case_id(split, digest, row):
    return f'unsmile-{split}-{digest[:12]}-{row}'


def choose(rows, *, digest, split, revision, seed, per_bucket, blocked):
    candidates, excluded, duplicates = {key: [] for key in BUCKETS}, 0, 0
    seen = set()
    # Prioritize region in multilabel rows; every row has one sampling bucket.
    for row in rows:
        key = row['normalizedSha256']
        if key in blocked:
            excluded += 1; continue
        if key in seen:
            duplicates += 1; continue
        seen.add(key)
        bucket = next((key for key in BUCKETS if row['labels'][key]), None)
        if bucket:
            candidates[bucket].append(row)
    picked, counts = [], {}
    for bucket, values in candidates.items():
        values.sort(key=lambda r: hashlib.sha256(f'{seed}:{split}:{digest}:{r["row"]}'.encode()).digest())
        if len(values) < per_bucket:
            raise ValueError('Not enough eligible rows for requested strata')
        counts[bucket] = per_bucket
        for row in values[:per_bucket]:
            picked.append(ReviewItem(id=case_id(split, digest, row['row']), sourceRow=row['row'],
                text=row['text'], originalLabels=row['labels'], normalizedSha256=row['normalizedSha256'],
                sourceFlags=source_flags(row['labels']), groupId='text-'+row['normalizedSha256']))
    review = ReviewSet(sourceRevision=revision, sourceSha256=digest, sourceSplit=split,
        purpose='development' if split == 'train' else 'confirmation', items=picked)
    return review, {'sourceRows': len(rows), 'selected': len(picked), 'primaryBuckets': counts,
                    'excludedNormalizedOverlap': excluded, 'withinSourceDuplicates': duplicates}


def prepare(train_path, valid_path, *, revision, seed, per_bucket=4, exclude_cases=()):
    if not isinstance(seed, str) or not seed.strip() or len(seed) > 100 or type(per_bucket) is not int or not 1 <= per_bucket <= 50:
        raise ValueError('Invalid sampling parameters')
    train_sha, train = read_source(train_path)
    valid_sha, valid = read_source(valid_path)
    excluded = {normalized_hash(c.input.target.text) for c in exclude_cases}
    development, dev_summary = choose(train, digest=train_sha, split='train', revision=revision,
        seed=seed, per_bucket=per_bucket, blocked=excluded)
    # Valid text seen anywhere in upstream train is not a fresh confirmation input.
    confirmation, hold_summary = choose(valid, digest=valid_sha, split='valid', revision=revision,
        seed=seed, per_bucket=per_bucket, blocked=excluded | {row['normalizedSha256'] for row in train})
    check_pair(development, confirmation)
    summary = {'kind': 'local-source-selection-only', 'sourceUrl': SOURCE_URL, 'sourceRevision': revision,
        'sourceSha256': {'train': train_sha, 'valid': valid_sha}, 'seed': seed, 'perBucket': per_bucket,
        'development': dev_summary, 'confirmation': hold_summary,
        'authoredDevelopmentInputsExcluded': len(excluded), 'serviceLabelsAssigned': 0,
        'modelCalls': 0, 'humanReviewCompleted': False,
        'limits': ['Stratified review sample, not source prevalence or performance evidence.',
                   'Normalized overlap checks do not detect paraphrases; review groupId manually.',
                   'Source labels are not service candidate/violation labels.',
                   'Confirmation text must not be used for prompt tuning.']}
    return development, confirmation, summary


def check_pair(one, two):
    if one.purpose == two.purpose or one.source_revision != two.source_revision:
        raise ValueError('Expected two purposes from same source revision')
    for attr in ('id', 'normalized_sha256', 'group_id'):
        left = {getattr(item, attr) for item in one.items}
        right = {getattr(item, attr) for item in two.items}
        if left & right:
            raise ValueError('Cross-split identity/text/family leakage')
    for review in (one, two):
        if len({item.normalized_sha256 for item in review.items}) != len(review.items):
            raise ValueError('Duplicate normalized source text')


def bind_source(review, path):
    digest, rows = read_source(path)
    if digest != review.source_sha256:
        raise ValueError('Source fingerprint changed')
    by_row = {row['row']: row for row in rows}
    for item in review.items:
        row = by_row.get(item.source_row)
        if (row is None or item.id != case_id(review.source_split, digest, item.source_row)
                or item.text != row['text'] or item.original_labels != row['labels']
                or item.normalized_sha256 != row['normalizedSha256']
                or item.source_flags != source_flags(row['labels'])):
            raise ValueError('Immutable source provenance was edited')


def export_cases(review, peer, *, source, peer_source):
    bind_source(review, source); bind_source(peer, peer_source); check_pair(review, peer)
    if any(item.status in {'PENDING', 'DISAGREEMENT'} for item in review.items):
        raise ValueError('Resolve every pending/disputed service expectation first')
    cases = []
    for item in review.items:
        if item.status != 'APPROVED':
            continue
        cases.append(EvalCase(id=item.id, input={'liveId': uuid5(NAMESPACE_URL, 'unsmile:'+review.source_revision),
            'target': {'messageId': uuid5(NAMESPACE_URL, item.id), 'text': item.text}, 'context': []},
            expectedCandidate=item.expected_candidate, expectedKinds=item.expected_kinds))
    if not cases:
        raise ValueError('No approved service cases')
    return cases, {'kind': 'local-reviewed-candidate-export', 'purpose': review.purpose,
        'sourceRevision': review.source_revision, 'sourceSha256': review.source_sha256,
        'datasetSha256': fingerprint(cases), 'selected': len(review.items), 'exported': len(cases),
        'excluded': [item.id for item in review.items if item.status == 'EXCLUDED'],
        'reviewAttribution': 'Self-reported review records; not authentication or policy approval.',
        'modelCalls': 0, 'enforcementAuthorized': False}


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2); stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--train', type=Path, required=True); prep.add_argument('--valid', type=Path, required=True)
    prep.add_argument('--revision', required=True); prep.add_argument('--seed', required=True)
    prep.add_argument('--per-bucket', type=int, default=4)
    prep.add_argument('--exclude-cases', type=Path, action='append', default=[])
    export = sub.add_parser('export')
    for name in ('review', 'peer', 'source', 'peer-source'):
        export.add_argument('--'+name, type=Path, required=True)
    for command in (prep, export): command.add_argument('--out-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.out_dir.exists():
            raise ValueError('Use a new output directory')
        if args.command == 'prepare':
            excluded = [case for path in args.exclude_cases for case in load_cases(path)]
            dev, hold, summary = prepare(args.train, args.valid, revision=args.revision, seed=args.seed,
                                         per_bucket=args.per_bucket, exclude_cases=excluded)
            files = {'development-review.json': dev.model_dump(mode='json', by_alias=True),
                     'confirmation-review.json': hold.model_dump(mode='json', by_alias=True),
                     'selection-summary.json': summary}
        else:
            review = ReviewSet.model_validate_json(args.review.read_text())
            peer = ReviewSet.model_validate_json(args.peer.read_text())
            cases, summary = export_cases(review, peer, source=args.source, peer_source=args.peer_source)
            files = {'cases.json': [c.model_dump(mode='json', by_alias=True) for c in cases], 'manifest.json': summary}
        args.out_dir.mkdir(parents=True, mode=0o700)
        for name, payload in files.items(): write_new(args.out_dir/name, payload)
    except (ValueError, OSError, TypeError, KeyError, csv.Error):
        parser.exit(2, 'Invalid local source/review or output; inspect privately, no exception text emitted.\n')
    print(json.dumps({'status': 'PREPARED' if args.command == 'prepare' else 'EXPORTED',
                      'modelCalls': 0, 'enforcementAuthorized': False}))
    return 0


if __name__ == '__main__': raise SystemExit(main())
