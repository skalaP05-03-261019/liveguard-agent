"""Investigation evaluator tests with scripted models and fixture tools; not model-quality evidence."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pydantic import ValidationError

from app.investigation.model import ToolCall
from app.investigation_eval.cases import CaseSet, FixtureBusiness, build_fixture, build_handoff, load_case_set
from app.investigation_eval.evaluate import (INVESTIGATOR_SOURCES, Prediction, UsageMeter, evaluate, outcome,
                                             score, source_fingerprint)
from app.phase2_bridge.handoff import ExistingInvestigator
from test_investigation import Policies, ScriptedModel, finish, search

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests/fixtures/phase2/investigation-eval-v1.json"
POLICY_VERSION = "fixture-policy-v1"


def case_set():
    return load_case_set(CASES)


def target_finding(case, verdict, categories=(), policy_ids=(), missing=("대상 확인 필요",)):
    target = build_fixture(case, POLICY_VERSION)["case"]["targetMessageIds"][0]
    return {"messageId": target, "verdict": verdict, "categories": list(categories), "policyIds": list(policy_ids),
            "evidenceMessageIds": [target], "reason": "통제된 테스트 판단",
            "missingFacts": list(missing) if verdict == "NEEDS_REVIEW" else []}


class CaseSetTests(unittest.TestCase):
    def test_fixture_cases_build_admissible_handoffs(self):
        data = case_set()
        self.assertEqual(data.review_status, "AUTHOR_DRAFT")
        self.assertEqual({c.split for c in data.cases}, {"development", "confirmation"})
        for case in data.cases:
            handoff = build_handoff(case, "v3.1")
            self.assertEqual(handoff.snapshot.handling_route, "CONTEXT_REVIEW")
            self.assertEqual([m.visibility for m in handoff.messages][-1], "HELD")
        # Deterministic IDs: the same case always becomes the same handoff.
        first = data.cases[0]
        self.assertEqual(build_fixture(first, "v3.1"), build_fixture(first, "v3.1"))

    def test_invalid_case_sets_are_rejected(self):
        raw = json.loads(CASES.read_text())
        broken = []
        dup = deepcopy(raw); dup["cases"].append(deepcopy(raw["cases"][0])); broken.append(dup)
        missing = deepcopy(raw); del missing["cases"][0]["expected"]["v3.1"]; broken.append(missing)
        no_category = deepcopy(raw)
        no_category["cases"][0]["expected"]["v3.2-draft"]["categories"] = []; broken.append(no_category)
        reserved = deepcopy(raw); reserved["cases"][0]["context"] = [{"text": "안녕", "author": "U1"}]; broken.append(reserved)
        uncertain = deepcopy(raw)
        uncertain["cases"][0]["hints"] = [{"kind": "GROUP_HATE", "strength": "UNCERTAIN"}]; broken.append(uncertain)
        for data in broken:
            with self.assertRaises(ValidationError):
                CaseSet.model_validate(data)


class CaseSetV2Tests(unittest.TestCase):
    def test_v2_keeps_v31_labels_and_maps_group_hate_to_cp33(self):
        v1 = {c.id: c for c in case_set().cases}
        v2 = load_case_set(ROOT / "tests/fixtures/phase2/investigation-eval-v2.json")
        self.assertEqual(set(v2.standards), {"v3.1", "v3.2"})
        for case in v2.cases:
            # v3.1 expectations are unchanged so Phase 1 comparisons stay valid.
            self.assertEqual(case.expected["v3.1"], v1[case.id].expected["v3.1"])
            if case.group in {"HATE", "INJECTION"}:
                expected = case.expected["v3.2"]
                self.assertEqual(([v.value for v in expected.verdicts], expected.categories, expected.policy_ids),
                                 (["VIOLATION"], ["HARASSMENT"], ["CP-3.3"]))

    def test_v3_applies_pm_review_only_where_marked(self):
        v2 = {c.id: c for c in load_case_set(ROOT / "tests/fixtures/phase2/investigation-eval-v2.json").cases}
        v3 = load_case_set(ROOT / "tests/fixtures/phase2/investigation-eval-v3.json")
        self.assertEqual((v3.version, v3.review_status), ("inv-eval-v3", "REVIEWED"))
        # BE rule-confirmed profanity is out of the investigation scope (PM review).
        self.assertEqual(set(v2) - {c.id for c in v3.cases}, {"C11", "C12"})
        changed = {(c.id, s) for c in v3.cases for s in v3.standards if c.expected[s] != v2[c.id].expected[s]}
        self.assertEqual(changed, {("D-P2-01", "v3.2"), ("D-P2-08", "v3.1"), ("D-P2-08", "v3.2"),
                                   ("C05", "v3.1"), ("C05", "v3.2")})
        for case_id, standard in changed:
            self.assertEqual([v.value for v in next(c for c in v3.cases if c.id == case_id)
                              .expected[standard].verdicts], ["NEEDS_REVIEW"])


class ScoringTests(unittest.TestCase):
    def setUp(self):
        self.cases = {c.id: c for c in case_set().cases}

    def test_outcome_classes(self):
        hate, normal, unclear = (self.cases[i] for i in ("D-P2-02", "D-P2-09", "D-P2-17"))
        draft = "v3.2-draft"
        self.assertEqual(outcome(hate.expected[draft], "VIOLATION"), "MATCH")
        self.assertEqual(outcome(hate.expected[draft], "NEEDS_REVIEW"), "HELD_VIOLATION")
        self.assertEqual(outcome(hate.expected[draft], "NO_VIOLATION"), "MISSED_VIOLATION")
        self.assertEqual(outcome(normal.expected[draft], "VIOLATION"), "FALSE_VIOLATION")
        self.assertEqual(outcome(normal.expected[draft], "NEEDS_REVIEW"), "OVER_HOLD")
        self.assertEqual(outcome(unclear.expected[draft], "NO_VIOLATION"), "UNSUPPORTED_NORMAL")
        # The same hold is correct under v3.1 and a weak result under the team draft.
        self.assertEqual(outcome(hate.expected["v3.1"], "NEEDS_REVIEW"), "MATCH")

    def test_definite_verdict_needs_expected_category_and_citation(self):
        hate = self.cases["D-P2-02"]
        ok = score(hate, "v3.2-draft", Prediction(verdict="VIOLATION", categories=["HATE"], policyIds=["CP-3.3"]))
        wrong_type = score(hate, "v3.2-draft", Prediction(verdict="VIOLATION", categories=["HARASSMENT"],
                                                           policyIds=["CP-3.3"]))
        no_cite = score(hate, "v3.2-draft", Prediction(verdict="VIOLATION", categories=["HATE"], policyIds=["CP-3.2"]))
        hold = score(hate, "v3.1", Prediction(verdict="NEEDS_REVIEW", missingFacts=["팀 정책"]))
        self.assertTrue(ok["satisfied"])
        self.assertEqual((wrong_type["categoryMatch"], wrong_type["satisfied"]), (False, False))
        self.assertEqual((no_cite["policyCited"], no_cite["satisfied"]), (False, False))
        self.assertTrue(hold["satisfied"])


class EvaluateTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_investigator_through_handoff_path(self):
        data = case_set()
        hate = next(c for c in data.cases if c.id == "D-P2-02")
        model = UsageMeter(ScriptedModel([finish([target_finding(hate, "NEEDS_REVIEW")])]))
        factory = lambda business: ExistingInvestigator(model=model, business=business, policy=Policies(),
                                                        observer=lambda event: None)
        report, details = await evaluate(data, [hate], "v3.2-draft", policy_version=POLICY_VERSION,
                                         investigator_factory=factory, meter=model)
        row = report["results"][0]
        self.assertEqual((row["status"], row["outcome"], row["modelCalls"]), ("ASSESSED", "HELD_VIOLATION", 1))
        self.assertEqual(report["usage"], {"inputTokens": 10, "outputTokens": 2})
        self.assertEqual(details[0]["missingFacts"], ["대상 확인 필요"])
        # A details row (minus the private reason) replays as a prediction under another standard.
        replay = {hate.id: {k: v for k, v in details[0].items() if k not in {"id", "reason"}}}
        again, _ = await evaluate(data, [hate], "v3.1", policy_version=POLICY_VERSION, predictions=replay)
        self.assertEqual(again["results"][0]["outcome"], "MATCH")
        # Reports never contain chat text or model reasons.
        dumped = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(hate.target, dumped)
        self.assertNotIn("통제된 테스트 판단", dumped)

    async def test_policy_backed_violation_with_wrong_category(self):
        data = case_set()
        variant = next(c for c in data.cases if c.id == "D-P2-06")
        finding = target_finding(variant, "VIOLATION", ["PROFANITY"], ["CP-2.1"])
        model = ScriptedModel([search(), finish([finding])])
        factory = lambda business: ExistingInvestigator(model=model, business=business, policy=Policies(),
                                                        observer=lambda event: None)
        report, _ = await evaluate(data, [variant], "v3.1", policy_version=POLICY_VERSION,
                                   investigator_factory=factory)
        row = report["results"][0]
        self.assertEqual((row["outcome"], row["categoryMatch"], row["policyCited"], row["satisfied"]),
                         ("MATCH", False, True, False))

    async def test_errors_are_codes_only(self):
        data = case_set()
        case = data.cases[0]

        class Broken:
            async def investigate(self, request):
                raise RuntimeError("private-error " + case.target)

        report, details = await evaluate(data, [case], "v3.1", policy_version=POLICY_VERSION,
                                         investigator_factory=lambda business: Broken())
        self.assertEqual((report["errors"], report["results"][0]["errorCode"]), (1, "INVESTIGATOR_ERROR"))
        self.assertEqual(details, [{"id": case.id, "status": "ERROR", "errorCode": "INVESTIGATOR_ERROR"}])
        # A recorded failure replays as the same failure under another standard.
        replay = {case.id: {k: v for k, v in details[0].items() if k != "id"}}
        again, _ = await evaluate(data, [case], "v3.2-draft", policy_version=POLICY_VERSION, predictions=replay)
        self.assertEqual((again["errors"], again["results"][0]["errorCode"]), (1, "INVESTIGATOR_ERROR"))
        malformed = {case.id: {"status": "ERROR", "errorCode": "not a code"}}
        again, _ = await evaluate(data, [case], "v3.1", policy_version=POLICY_VERSION, predictions=malformed)
        self.assertEqual(again["results"][0]["errorCode"], "INVESTIGATOR_ERROR")
        self.assertNotIn(case.target, json.dumps(report, ensure_ascii=False))

    async def test_offline_predictions_and_summary(self):
        data = case_set()
        cases = [c for c in data.cases if c.split == "development"]
        predictions = {c.id: {"verdict": "NEEDS_REVIEW", "missingFacts": ["x"]} for c in cases}
        report, _ = await evaluate(data, cases, "v3.1", policy_version="v3.1", predictions=predictions)
        self.assertEqual((report["total"], report["assessed"], report["errors"]), (20, 20, 0))
        self.assertEqual(set(report["bySplit"]), {"development"})
        self.assertEqual(report["outcomes"]["OVER_HOLD"] + report["outcomes"]["HELD_VIOLATION"]
                         + report["outcomes"]["MATCH"], 20)
        with self.assertRaises(ValueError):
            await evaluate(data, cases, "v3.1", policy_version="v3.1", predictions={})
        with self.assertRaises(ValueError):
            await evaluate(data, cases, "unknown", policy_version="v3.1", predictions=predictions)


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        return subprocess.run([sys.executable, "-m", "app.investigation_eval", *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)

    def test_offline_run_defaults_to_development_and_never_overwrites(self):
        data = case_set()
        with tempfile.TemporaryDirectory() as tmp:
            predictions = Path(tmp) / "p.json"
            predictions.write_text(json.dumps({c.id: {"verdict": "NEEDS_REVIEW", "missingFacts": ["x"]}
                                               for c in data.cases if c.split == "development"}))
            out = Path(tmp) / "report.json"
            args = ["--cases", str(CASES), "--standard", "v3.1", "--predictions", str(predictions),
                    "--policy-version", "v3.1", "--out", str(out)]
            first = self.run_cli(*args)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(out.read_text())["total"], 20)
            again = self.run_cli(*args)
            self.assertNotEqual(again.returncode, 0)

    def test_live_requires_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_cli("--cases", str(CASES), "--standard", "v3.1", "--live", "--policy-version", "v3.1",
                                  "--out", str(Path(tmp) / "r.json"))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(tmp) / "r.json").exists())


class SourceFingerprintTests(unittest.TestCase):
    def test_all_investigator_sources_are_hashed(self):
        info = source_fingerprint()
        self.assertEqual(set(info["files"]), set(INVESTIGATOR_SOURCES))
        self.assertRegex(info["investigatorSourcesSha256"], r"^[a-f0-9]{64}$")


class FixtureBusinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_are_bound_to_trace(self):
        fixture = build_fixture(case_set().cases[0], "v3.1")
        business = FixtureBusiness(fixture)
        response = await business.call("get_stream_info", {"caseId": "x", "traceId": "t"})
        self.assertEqual((response["traceId"], response["data"]["authorRole"]), ("t", "UNKNOWN"))
        self.assertEqual(business.calls, ["get_stream_info"])


if __name__ == "__main__":
    unittest.main()
