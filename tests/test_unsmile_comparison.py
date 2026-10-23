"""The pickle experiment must not turn a model score into a policy action."""

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.candidate_detection.compare_web import create_comparison_app
from app.candidate_detection.contracts import Assessment
from app.candidate_detection.unsmile_pickle import LABELS, UnSmilePickle


class FakeModel:
    def __init__(self, labels=LABELS):
        self.config = types.SimpleNamespace(id2label=dict(enumerate(labels)))
        self.evaluated = False

    def eval(self):
        self.evaluated = True

    def __call__(self, **inputs):
        return types.SimpleNamespace(logits=[0.1] * 10)


class FakeTorch:
    @staticmethod
    def inference_mode():
        from contextlib import nullcontext
        return nullcontext()

    @staticmethod
    def sigmoid(logits):
        class Scores:
            def squeeze(self, axis):
                return self

            def tolist(self):
                return [0.8, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.2, 0.9]
        return Scores()


class TestUnSmilePickle(unittest.TestCase):
    def test_scores_keep_clean_separate_from_hate_labels(self):
        model = FakeModel()
        classifier = UnSmilePickle(model, lambda text, **kw: {'input_ids': [1]})
        with patch.dict(sys.modules, {'torch': FakeTorch}):
            result = classifier.predict('시험 문장')
        self.assertTrue(model.evaluated)
        self.assertTrue(result['selected'])
        self.assertEqual(result['matchedLabels'], ['여성/가족'])
        self.assertEqual(result['scores']['clean'], 0.9)
        self.assertIsNone(result['policyVerdict'])

    def test_rejects_wrong_label_order_and_implicit_pickle_trust(self):
        with self.assertRaises(ValueError):
            UnSmilePickle(FakeModel(tuple(reversed(LABELS))), lambda text: None)
        with self.assertRaises(ValueError):
            UnSmilePickle.load(Path('/tmp/does-not-exist.pkl'), 'beomi/kcbert-base')


class FakeScreen:
    async def assess(self, data, *, trace_id):
        return Assessment(signals=[]), {'inputTokens': 12}


class FakeLocal:
    def predict(self, text):
        return {'selected': True, 'matchedLabels': ['악플/욕설'],
                'scores': {'악플/욕설': 0.88, 'clean': 0.06},
                'threshold': 0.5, 'scope': 'target-text-only', 'policyVerdict': None}


class BrokenLocal:
    def predict(self, text):
        raise RuntimeError('private model failure')


def request_body():
    return {'requestId': str(uuid4()), 'experiment': 'clear-or-possible',
            'input': {'liveId': str(uuid4()), 'target': {'messageId': str(uuid4()), 'text': '씨바'},
                      'context': []}}


class TestComparisonWeb(unittest.TestCase):
    def test_same_target_returns_two_separate_read_only_results(self):
        with TestClient(create_comparison_app(FakeScreen(), FakeLocal()), base_url='http://127.0.0.1:8766') as client:
            response = client.post('/api/compare', json=request_body(),
                                   headers={'Origin': 'http://127.0.0.1:8766'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['gpt']['selected'])
        self.assertTrue(data['unsmile']['selected'])
        self.assertEqual(data['unsmile']['scope'], 'target-text-only')
        self.assertFalse(data['executionAuthorized'])

    def test_model_error_isolated_and_origin_enforced(self):
        with TestClient(create_comparison_app(FakeScreen(), BrokenLocal()), base_url='http://127.0.0.1:8766') as client:
            rejected = client.post('/api/compare', json=request_body(),
                                   headers={'Origin': 'https://evil.example'})
            response = client.post('/api/compare', json=request_body(),
                                   headers={'Origin': 'http://127.0.0.1:8766'})
        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['gpt']['status'], 'OK')
        self.assertEqual(response.json()['unsmile']['errorCode'], 'UNSMILE_INFERENCE_FAILED')
        self.assertNotIn('private model failure', response.text)
