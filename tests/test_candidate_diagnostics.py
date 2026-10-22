"""Synthetic-only development diagnostics; never human-reviewed quality claims."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.candidate_detection.curation import ReviewSet, prepare
from app.candidate_detection.diagnose import diagnose, development_inputs, main, review_queue
from app.candidate_detection.screen import CandidateScreen
from app.clients.business_mcp import AgentToolError
from app.investigation.model import ModelTurn, ToolCall
from test_candidate_curation import write_source


class CaptureModel:
    def __init__(self): self.calls = []

    async def complete(self, messages, tools, *, trace_id):
        self.calls.append((messages, tools))
        return ModelTurn([ToolCall('normal', 'report_candidate_signals', {'signals': []})])


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.train, self.valid = self.folder/'train.tsv', self.folder/'valid.tsv'
        write_source(self.train, 'train'); write_source(self.valid, 'valid')
        dev, self.hold, _ = prepare(self.train, self.valid, revision='a'*40, seed='diagnostics', per_bucket=1)
        raw = dev.model_dump(mode='json', by_alias=True)
        for i, item in enumerate(raw['items']):
            item['proposal'] = {'kind': 'AI_DRAFT', 'expectedCandidate': [True, False, None][i%3],
                                'rationale': 'Private draft marker not a service label'}
        self.dev = ReviewSet.model_validate(raw)
        self.predictions = {item.id: {'signals': []} for item in self.dev.items}

    def run_diagnostic(self, **kwargs):
        return asyncio.run(diagnose(self.dev, source=self.train,
                                   **({'predictions': self.predictions} | kwargs)))

    def test_unreviewed_draft_comparison_has_no_ground_truth_metrics_or_approval(self):
        before = self.dev.model_dump_json()
        report = self.run_diagnostic(); summary = report['summary']
        self.assertEqual(summary['total'], 10); self.assertEqual(summary['assessed'], 10)
        comparison = summary['aiDraftComparison']['clear-or-possible']
        self.assertEqual(comparison, {'DIFFERS_FROM_AI_DRAFT': 4, 'AGREES_WITH_AI_DRAFT': 3, 'UNRESOLVED': 3})
        self.assertFalse(summary['serviceMetricsComputed']); self.assertFalse(summary['enforcementAuthorized'])
        self.assertFalse(summary['usesHumanGroundTruthForScoring'])
        self.assertEqual(summary['reviewStatuses'], {'PENDING': 10})
        self.assertEqual(before, self.dev.model_dump_json())
        self.assertFalse({'precision', 'recall', 'f1', 'accuracy', 'tp', 'fn'} & set(summary))

    def test_confirmation_and_tampered_source_are_rejected_before_model(self):
        model = CaptureModel()
        with self.assertRaises(ValueError):
            asyncio.run(diagnose(self.hold, source=self.valid, screen=CandidateScreen(model), limit=1))
        raw = self.dev.model_dump(mode='json', by_alias=True); raw['items'][0]['text'] = 'forged'
        with self.assertRaises(ValueError):
            asyncio.run(diagnose(ReviewSet.model_validate(raw), source=self.train,
                                 screen=CandidateScreen(model), limit=1))
        self.assertEqual(model.calls, [])

    def test_detector_receives_only_original_inputs_not_source_labels_or_drafts(self):
        model = CaptureModel()
        report = asyncio.run(diagnose(self.dev, source=self.train, screen=CandidateScreen(model), limit=2))
        self.assertEqual(len(model.calls), 2); self.assertEqual(report['summary']['total'], 2)
        for messages, _ in model.calls:
            user = json.loads(messages[1]['content'])
            self.assertEqual(set(user), {'dataKind', 'input'})
            self.assertEqual(set(user['input']), {'liveId', 'target', 'context'})
            self.assertEqual(user['input']['context'], [])
            self.assertNotIn('Private draft marker', json.dumps(messages))

    def test_limits_and_prediction_ids_are_strict(self):
        for limit in (0, 101, True):
            with self.assertRaises(ValueError): self.run_diagnostic(limit=limit)
        with self.assertRaises(ValueError): self.run_diagnostic(predictions={})
        with self.assertRaises(ValueError): self.run_diagnostic(predictions=[], limit=1)
        with self.assertRaises(ValueError):
            asyncio.run(diagnose(self.dev, source=self.train, screen=CandidateScreen(CaptureModel())))
        with self.assertRaises(ValueError):
            asyncio.run(diagnose(self.dev, source=self.train))

    def test_invented_quotes_and_action_fields_are_errors_not_normal_findings(self):
        first, data = development_inputs(self.dev, self.train)[0]
        self.predictions[first.id] = {'signals': [{'kind': 'GROUP_HATE', 'strength': 'CLEAR',
            'reason': 'Synthetic reason', 'evidence': [{'messageId': str(data.target.message_id), 'text': 'invented'}]}]}
        second = self.dev.items[1].id; self.predictions[second] = {'signals': [], 'kickUser': True}
        report = self.run_diagnostic()
        self.assertEqual(report['summary']['assessed'], 8); self.assertEqual(report['summary']['errors'], 2)
        self.assertEqual(sum(report['summary']['aiDraftComparison']['clear-only'].values()), 8)
        self.assertEqual([r['status'] for r in report['results'][:2]], ['ERROR', 'ERROR'])

    def test_fatal_auth_error_stops_remaining_calls_and_hides_exception_text(self):
        class BrokenScreen:
            calls = 0
            async def assess(self, data, *, trace_id):
                self.calls += 1
                raise AgentToolError('UNAUTHENTICATED', trace_id=trace_id)
        screen = BrokenScreen()
        report = asyncio.run(diagnose(self.dev, source=self.train, screen=screen, limit=3))
        self.assertEqual(screen.calls, 1); self.assertEqual(report['summary']['notRun'], 2)
        self.assertEqual(report['summary']['errors'], 1)

    def test_unexpected_error_details_never_enter_report_or_stdout(self):
        class BrokenScreen:
            async def assess(self, *args, **kwargs): raise RuntimeError('SECRET model response')
        report = asyncio.run(diagnose(self.dev, source=self.train, screen=BrokenScreen(), limit=1))
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertEqual(report['results'][0]['errorCode'], 'DETECTOR_ERROR')

    def test_signal_strengths_produce_different_selection_observations(self):
        item, data = development_inputs(self.dev, self.train)[0]
        self.predictions[item.id] = {'signals': [{'kind': 'GROUP_HATE', 'strength': 'POSSIBLE',
            'reason': 'Synthetic candidate only', 'evidence': [{'messageId': str(data.target.message_id), 'text': data.target.text}]}]}
        report = self.run_diagnostic()
        self.assertEqual(report['summary']['candidateCounts'],
                         {'clear-only': 0, 'clear-or-possible': 1, 'include-uncertain': 1})

    def test_safe_summary_has_no_private_text_or_per_row_identifiers(self):
        report = self.run_diagnostic(); summary = json.dumps(report['summary'], ensure_ascii=False)
        for item in self.dev.items:
            self.assertNotIn(item.text, summary); self.assertNotIn(item.id, summary)
        self.assertNotIn('Private draft marker', summary)
        queue = review_queue(self.dev, report)
        self.assertIn(self.dev.items[0].text, queue)
        self.assertIn('자동 승인 아님', queue)

    def test_cli_private_outputs_no_overwrite_and_no_source_modification(self):
        review_path, predictions_path = self.folder/'dev.json', self.folder/'predictions.json'
        review_path.write_text(self.dev.model_dump_json(by_alias=True))
        predictions_path.write_text(json.dumps(self.predictions))
        original = review_path.read_bytes(); output = self.folder/'diagnosis'
        argv = ['diagnose', '--review', str(review_path), '--source', str(self.train),
                '--predictions', str(predictions_path), '--out-dir', str(output)]
        with patch('sys.argv', argv), redirect_stdout(io.StringIO()): self.assertEqual(main(), 0)
        self.assertEqual(original, review_path.read_bytes())
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        for path in output.iterdir(): self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with patch('sys.argv', argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main()

    def test_cli_rejects_confirmation_before_model_init_or_output(self):
        path = self.folder/'confirmation.json'; path.write_text(self.hold.model_dump_json(by_alias=True))
        output = self.folder/'blocked'
        argv = ['diagnose', '--review', str(path), '--source', str(self.valid), '--live', '--limit', '1', '--out-dir', str(output)]
        with patch('sys.argv', argv), patch('app.candidate_detection.diagnose.model_from_env') as factory:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit): main()
            factory.assert_not_called()
        self.assertFalse(output.exists())
