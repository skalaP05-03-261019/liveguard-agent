"""Paired metrics integrity; scripted predictions are not classifier quality evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.candidate_detection.compare import compare_reports, main
from app.candidate_detection.contracts import EXPERIMENTS
from app.candidate_detection.evaluate import load_cases, fingerprint

PATH = Path(__file__).parent/'fixtures/phase2/metrics-smoke-cases.json'


def report(cases, selections):
    rows = []
    for c, chosen in zip(cases, selections):
        rows.append({'id': c.id, 'expectedCandidate': c.expected_candidate,
                     'status': 'ASSESSED', 'riskKinds': ['TARGETED_ATTACK'] if chosen else [],
                     'candidates': {name: chosen for name in EXPERIMENTS}})
    return {'kind': 'candidate-screening-only', 'datasetSha256': fingerprint(cases),
            'total': len(rows), 'assessed': len(rows), 'errors': 0, 'results': rows,
            'execution': {'mode': 'offline-predictions'}}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases(PATH)

    def test_fixed_and_new_false_positives_and_negatives_have_exact_ids(self):
        # Reverse each expected label, then get each right.
        old = report(self.cases, [not c.expected_candidate for c in self.cases])
        new = report(self.cases, [c.expected_candidate for c in self.cases])
        r = compare_reports(old, new, cases=self.cases)
        for name in EXPERIMENTS:
            result = r['experiments'][name]
            self.assertEqual(result['pairedAfter']['fp'], 0)
            self.assertEqual(result['pairedAfter']['fn'], 0)
            self.assertEqual(set(result['changes']['fixedFalsePositives']),
                             {c.id for c in self.cases if not c.expected_candidate})
            self.assertEqual(set(result['changes']['fixedFalseNegatives']),
                             {c.id for c in self.cases if c.expected_candidate})
        reverse = compare_reports(new, old, cases=self.cases)['experiments']['clear-only']['changes']
        self.assertTrue(reverse['newFalsePositives']); self.assertTrue(reverse['newFalseNegatives'])

    def test_errors_are_separate_and_both_metrics_use_common_rows(self):
        a = report(self.cases, [True]*4); b = deepcopy(a)
        a['results'][0] = {**a['results'][0], 'status': 'ERROR'}
        b['results'][1] = {**b['results'][1], 'status': 'ERROR'}
        a['results'][2] = {**a['results'][2], 'status': 'ERROR'}
        b['results'][2] = {**b['results'][2], 'status': 'ERROR'}
        for r in (a, b): r.update(assessed=2, errors=2)
        result = compare_reports(a, b, cases=self.cases)
        self.assertEqual(result['pairedAssessed'], 1)
        self.assertEqual(result['pairedCoverage'], .25)
        self.assertEqual(result['errors'], {'recoveredFromError':[self.cases[0].id],
            'becameError':[self.cases[1].id], 'stayedError':[self.cases[2].id]})
        self.assertEqual(result['experiments']['clear-only']['changes']['fixedFalsePositives'], [])

    def test_dataset_labels_order_counts_and_ids_cannot_be_silently_changed(self):
        a = report(self.cases, [True]*4)
        edits = [lambda b:b.update(datasetSha256='0'*64),
                 lambda b:b['results'].reverse(),
                 lambda b:b['results'][0].update(expectedCandidate=not b['results'][0]['expectedCandidate']),
                 lambda b:b['results'][0].update(id=b['results'][1]['id']),
                 lambda b:b.update(total=99),lambda b:b.update(assessed=True)]
        for edit in edits:
            with self.subTest(edit=edit):
                b=deepcopy(a);edit(b)
                with self.assertRaises(ValueError):compare_reports(a,b,cases=self.cases)
        changed = [c.model_copy(deep=True) for c in self.cases]
        changed[0].input.target.text = 'Changed input must fail even with identical report labels'
        with self.assertRaises(ValueError):compare_reports(a,a,cases=changed)

    def test_invalid_status_choices_and_risk_types_are_rejected(self):
        a=report(self.cases,[True]*4)
        for field,value in [('status','MAYBE'),('expectedCandidate',1),('riskKinds',['NEW_CATEGORY']),
                            ('riskKinds',['TARGETED_ATTACK','TARGETED_ATTACK']),
                            ('candidates',{'clear-only':True}),
                            ('candidates',{'clear-only':True,'clear-or-possible':False,'include-uncertain':True})]:
            b=deepcopy(a);b['results'][0][field]=value
            with self.assertRaises(ValueError):compare_reports(a,b,cases=self.cases)

    def test_groups_require_matching_dataset_and_cover_every_case(self):
        a=report(self.cases,[True]*4)
        groups={'version':'candidate-groups-v1','datasetSha256':fingerprint(self.cases),
                'cases':{c.id:['normal' if not c.expected_candidate else 'attack','shared'] for c in self.cases}}
        r=compare_reports(a,a,cases=self.cases,groups=groups)
        slices=r['experiments']['clear-only']['slices']
        self.assertEqual(slices['shared']['total'],4)
        self.assertEqual(slices['normal']['before']['fp'],2)
        for edit in (lambda g:g.update(datasetSha256='0'*64),lambda g:g['cases'].pop(self.cases[0].id),
                     lambda g:g['cases'].update({self.cases[0].id:['raw private text']})):
            bad=deepcopy(groups);edit(bad)
            with self.assertRaises(ValueError):compare_reports(a,a,cases=self.cases,groups=bad)

    def test_no_successful_pairs_does_not_report_perfect_accuracy(self):
        a=report(self.cases,[True]*4)
        for row in a['results']:row['status']='ERROR'
        a.update(assessed=0,errors=4)
        r=compare_reports(a,a,cases=self.cases)
        self.assertEqual(r['pairedAssessed'],0)
        self.assertIsNone(r['experiments']['clear-only']['pairedAfter']['f1'])

    def test_summary_omits_input_text_reason_error_and_arbitrary_execution_fields(self):
        a=report(self.cases,[True]*4)
        a['execution']['private']='private-secret'
        a['results'][0].update(reason='private-reason',errorCode='private-error',text='private-chat')
        r=compare_reports(a,a,cases=self.cases)
        self.assertNotIn('private-',json.dumps(r))
        for c in self.cases:self.assertNotIn(c.input.target.text,json.dumps(r,ensure_ascii=False))

    def test_cli_writes_private_new_file_and_refuses_overwrite(self):
        a=report(self.cases,[True]*4)
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.json';source.write_text(json.dumps(a))
            out=Path(folder)/'report.json'
            argv=['compare','--before',str(source),'--after',str(source),'--cases',str(PATH),'--out',str(out)]
            with patch('sys.argv',argv):self.assertEqual(main(),0)
            self.assertEqual(out.stat().st_mode&0o777,0o600)
            original=out.read_bytes()
            with patch('sys.argv',argv),self.assertRaises(SystemExit):main()
            self.assertEqual(out.read_bytes(),original)
