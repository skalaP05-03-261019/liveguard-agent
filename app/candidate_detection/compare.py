"""Paired candidate experiments on identical cases; never compare unlike denominators silently."""

import argparse
import json
import os
from pathlib import Path
import re
from .contracts import EXPERIMENTS, RiskKind
from .evaluate import confusion, ratio, fingerprint, load_cases


def checked_rows(report):
    if (not isinstance(report, dict) or report.get('kind') != 'candidate-screening-only'
            or not isinstance(report.get('datasetSha256'), str)
            or not re.fullmatch(r'[a-f0-9]{64}', report['datasetSha256'])):
        raise ValueError('Invalid report identity')
    rows = report.get('results')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('Expected 1..1000 results')
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid row')
        key = row.get('id')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) or key in seen:
            raise ValueError('Invalid or duplicate ID')
        seen.add(key)
        if type(row.get('expectedCandidate')) is not bool or row.get('status') not in {'ASSESSED', 'ERROR'}:
            raise ValueError('Invalid label or status')
        if row['status'] == 'ASSESSED':
            choices = row.get('candidates')
            kinds = row.get('riskKinds')
            if (not isinstance(choices, dict) or set(choices) != set(EXPERIMENTS)
                    or any(type(value) is not bool for value in choices.values())
                    or not isinstance(kinds, list) or any(type(kind) is not str for kind in kinds)
                    or len(kinds) != len(set(kinds)) or not set(kinds) <= {k.value for k in RiskKind}):
                raise ValueError('Invalid assessed result')
            if (not choices['clear-only'] <= choices['clear-or-possible'] <= choices['include-uncertain']
                    or choices['include-uncertain'] != bool(kinds)):
                raise ValueError('Inconsistent selection criteria')
    if (any(type(report.get(k)) is not int for k in ('total', 'assessed', 'errors'))
            or report['total'] != len(rows)
            or report.get('assessed') != sum(r['status'] == 'ASSESSED' for r in rows)
            or report.get('errors') != sum(r['status'] == 'ERROR' for r in rows)):
        raise ValueError('Invalid counts')
    return rows


def checked_groups(groups, ids, dataset_sha):
    if groups is None:
        return {key: ['all'] for key in ids}
    if (not isinstance(groups, dict) or groups.get('version') != 'candidate-groups-v1'
            or groups.get('datasetSha256') != dataset_sha
            or not isinstance(groups.get('cases'), dict) or set(groups['cases']) != set(ids)):
        raise ValueError('Group manifest must match exact dataset and IDs')
    for tags in groups['cases'].values():
        if (not isinstance(tags, list) or not 1 <= len(tags) <= 8
                or any(not isinstance(t, str) or not re.fullmatch(r'[a-z0-9_-]{1,64}', t) for t in tags)
                or len(tags) != len(set(tags))):
            raise ValueError('Invalid group tags')
    return groups['cases']


def source_summary(report):
    # Do not copy arbitrary execution/error payloads into the public summary.
    execution = report.get('execution') or {}
    if not isinstance(execution, dict):
        raise ValueError('Invalid execution metadata')
    prompt = execution.get('promptSha256')
    if prompt is not None and (not isinstance(prompt, str) or not re.fullmatch(r'[a-f0-9]{64}', prompt)):
        raise ValueError('Invalid prompt identity')
    mode = execution.get('mode')
    if mode not in {None, 'live-model', 'offline-predictions'}:
        raise ValueError('Invalid execution mode')
    model = execution.get('sourceModel')
    if model is not None and (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', model)):
        raise ValueError('Invalid model identity')
    return {'mode': mode, 'model': model, 'promptSha256': prompt}


def compare_reports(before, after, *, cases, groups=None):
    left, right = checked_rows(before), checked_rows(after)
    if before['datasetSha256'] != after['datasetSha256'] or [r['id'] for r in left] != [r['id'] for r in right]:
        raise ValueError('Compare the exact same ordered dataset')
    if any(a['expectedCandidate'] != b['expectedCandidate'] for a, b in zip(left, right)):
        raise ValueError('Expected labels changed')
    if (fingerprint(cases) != before['datasetSha256'] or [c.id for c in cases] != [r['id'] for r in left]
            or any(c.expected_candidate != r['expectedCandidate'] for c, r in zip(cases, left))):
        raise ValueError('Reports must match original cases and labels')
    tags = checked_groups(groups, [r['id'] for r in left], before['datasetSha256'])
    paired = [(a, b) for a, b in zip(left, right) if a['status'] == b['status'] == 'ASSESSED']
    experiments = {}
    for name in EXPERIMENTS:
        metrics = lambda rows: confusion([(r['expectedCandidate'], r['candidates'][name]) for r in rows])
        changes = {key: [] for key in ('fixedFalsePositives', 'newFalsePositives',
                                       'fixedFalseNegatives', 'newFalseNegatives')}
        for a, b in paired:
            old, new, expected = a['candidates'][name], b['candidates'][name], a['expectedCandidate']
            if old != new:
                key = ('fixedFalseNegatives' if new else 'newFalseNegatives') if expected else (
                    'newFalsePositives' if new else 'fixedFalsePositives')
                changes[key].append(a['id'])
        slices = {}
        for tag in sorted({tag for values in tags.values() for tag in values}):
            rows = [(a, b) for a, b in paired if tag in tags[a['id']]]
            all_ids = [a['id'] for a in left if tag in tags[a['id']]]
            slices[tag] = {'total': len(all_ids), 'pairedAssessed': len(rows),
                           'before': metrics([a for a, b in rows]), 'after': metrics([b for a, b in rows])}
        experiments[name] = {
            'beforeAssessedOnly': metrics([r for r in left if r['status'] == 'ASSESSED']),
            'afterAssessedOnly': metrics([r for r in right if r['status'] == 'ASSESSED']),
            'pairedBefore': metrics([a for a, b in paired]),
            'pairedAfter': metrics([b for a, b in paired]), 'changes': changes, 'slices': slices}
    return {'kind': 'paired-candidate-comparison', 'datasetSha256': before['datasetSha256'],
            'total': len(left), 'pairedAssessed': len(paired), 'pairedCoverage': ratio(len(paired), len(left)),
            'sources': {'before': source_summary(before), 'after': source_summary(after)},
            'errors': {'becameError': [a['id'] for a, b in zip(left, right) if a['status'] == 'ASSESSED' and b['status'] == 'ERROR'],
                       'recoveredFromError': [a['id'] for a, b in zip(left, right) if a['status'] == 'ERROR' and b['status'] == 'ASSESSED'],
                       'stayedError': [a['id'] for a, b in zip(left, right) if a['status'] == b['status'] == 'ERROR']},
            'experiments': experiments,
            'limits': ['Paired metrics exclude errors; show total/coverage and error transitions together.',
                       'Overlapping group counts must not be summed.',
                       'Development cases and repeated runs do not establish held-out quality or causal improvement.',
                       'Selection is not a violation, customer policy, Backend delivery or enforcement.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--groups', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.exit(2, 'Output exists; choose a new file.\n')
    try:
        report = compare_reports(json.loads(args.before.read_text()), json.loads(args.after.read_text()),
                                 cases=load_cases(args.cases),
                                 groups=json.loads(args.groups.read_text()) if args.groups else None)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2); stream.write('\n')
    except (ValueError, OSError, TypeError, KeyError):
        parser.exit(2, 'Invalid report, group manifest or output; no valid comparison produced.\n')
    print(json.dumps({'kind': report['kind'], 'total': report['total'], 'pairedAssessed': report['pairedAssessed']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
