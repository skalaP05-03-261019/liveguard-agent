"""Phase 2 investigator wiring with scripted models; not model-quality evidence."""

import json
from pathlib import Path
import unittest

from app.clients.business_mcp import AgentToolError
from app.investigation.runner import PROMPT, run_investigation
from app.investigation_eval.cases import FixtureBusiness, build_fixture, build_handoff, load_case_set
from app.investigation_eval.evaluate import evaluate
from app.phase2_bridge.handoff import ExistingInvestigator, run_readonly_investigation
from app.investigation.runner import prompt_for_policy
from app.phase2_investigation.investigator import (DEFAULT_SECTION, VERSION_MARKER, VERSION_SECTIONS,
                                                  Phase2Investigator, candidate_hints, system_prompt)
from test_investigation import Policies, ScriptedModel, finish
from test_investigation_eval import CASES, POLICY_VERSION, target_finding

STRUCTURED = "대상: 집단(출신 지역). 표현: 열등 단정. 문맥: 일반 발화. 판단: CP-6.1에 따라 운영자 검토."


def case(case_id):
    return next(c for c in load_case_set(CASES).cases if c.id == case_id)


class Phase2InvestigatorTests(unittest.IsolatedAsyncioTestCase):
    async def run_one(self, investigator_cls, item, findings):
        model = ScriptedModel([finish(findings)])
        investigator = investigator_cls(model=model, business=FixtureBusiness(build_fixture(item, POLICY_VERSION)),
                                        policy=Policies(), observer=lambda event: None)
        draft = await run_readonly_investigation(build_handoff(item, POLICY_VERSION), investigator)
        return draft, model

    async def test_prompt_and_hints_reach_the_model(self):
        item = case("D-P2-17")
        finding = target_finding(item, "NEEDS_REVIEW", missing=["'저쪽'이 가리키는 대상"])
        draft, model = await self.run_one(Phase2Investigator, item, [finding])
        messages, _, _ = model.inputs[0]
        self.assertEqual(messages[0]["content"], system_prompt(POLICY_VERSION))
        self.assertTrue(messages[0]["content"].startswith(PROMPT.read_text().rstrip()))
        # Non-v3.2 cases keep the v3.1 section (pure group hate stays CP-6.1 review).
        self.assertIn(DEFAULT_SECTION.read_text().strip(), messages[0]["content"])
        self.assertNotIn(VERSION_MARKER, messages[0]["content"])
        first = json.loads(messages[1]["content"])
        self.assertEqual(first["candidateHints"]["signals"], [{"kind": "GROUP_HATE", "strength": "UNCERTAIN"}])
        self.assertEqual(first["candidateHints"]["missingFacts"], ["'저쪽'이 가리키는 대상"])
        # Detector reasons are not forwarded; evidence and policy version are unchanged.
        self.assertNotIn("평가 사례에 고정된 후보 힌트", messages[1]["content"])
        self.assertEqual(first["policyVersion"], POLICY_VERSION)
        self.assertEqual(draft.analysis.message_findings[0].verdict, "NEEDS_REVIEW")
        self.assertEqual(draft.harness_budget["modelCalls"], 1)

    async def test_phase1_investigator_is_unchanged(self):
        item = case("D-P2-17")
        _, model = await self.run_one(ExistingInvestigator, item, [target_finding(item, "NEEDS_REVIEW")])
        messages, _, _ = model.inputs[0]
        self.assertEqual(messages[0]["content"], PROMPT.read_text())
        self.assertNotIn("candidateHints", json.loads(messages[1]["content"]))

    async def test_extra_input_cannot_replace_evidence(self):
        handoff = build_handoff(case("D-P2-01"), POLICY_VERSION)
        for key in ("policyVersion", "targetMessages", "dataKind"):
            with self.assertRaises(AgentToolError) as raised:
                await run_investigation(handoff.agent_state(), model=ScriptedModel([]), business=None,
                                        policy=Policies(), extra_input={key: "x"})
            self.assertEqual(raised.exception.code, "INVALID_INPUT")

    async def test_failures_are_not_findings(self):
        item = case("D-P2-01")

        class Broken:
            async def complete(self, messages, tools, *, trace_id):
                raise AgentToolError("MODEL_UNAVAILABLE", trace_id=trace_id, retryable=False)

        investigator = Phase2Investigator(model=Broken(), business=FixtureBusiness(build_fixture(item, POLICY_VERSION)),
                                          policy=Policies(), observer=lambda event: None)
        with self.assertRaises(AgentToolError) as raised:
            await run_readonly_investigation(build_handoff(item, POLICY_VERSION), investigator)
        self.assertEqual(raised.exception.code, "MODEL_UNAVAILABLE")

    async def test_evaluator_counts_structured_reasons(self):
        data = load_case_set(CASES)
        item = case("D-P2-02")
        finding = {**target_finding(item, "NEEDS_REVIEW"), "reason": STRUCTURED}
        model = ScriptedModel([finish([finding])])
        factory = lambda business: Phase2Investigator(model=model, business=business, policy=Policies(),
                                                      observer=lambda event: None)
        report, _ = await evaluate(data, [item], "v3.1", policy_version=POLICY_VERSION, investigator_factory=factory)
        self.assertEqual((report["structuredReasons"], report["results"][0]["outcome"]), (1, "MATCH"))
        self.assertNotIn(STRUCTURED, json.dumps(report, ensure_ascii=False))

    def test_v32_cases_use_v32_prompt_and_group_section(self):
        v31, v32 = system_prompt("v3.1"), system_prompt("v3.2")
        self.assertTrue(v32.startswith(prompt_for_policy("v3.2").rstrip()))
        self.assertIn(VERSION_SECTIONS["v3.2"].read_text().strip(), v32)
        self.assertIn("CP-3.3", v32)
        self.assertNotIn("집단 혐오를 HARASSMENT로 바꿔 판정하지 마세요", v32)
        # v3.1 never sees CP-3.3 instructions; the rule is not backported.
        self.assertIn("집단 혐오를 HARASSMENT로 바꿔 판정하지 마세요", v31)
        self.assertNotIn("CP-3.3", v31)

    def test_hints_carry_kinds_and_strengths_only(self):
        handoff = build_handoff(case("D-P2-06"), POLICY_VERSION)
        hints = candidate_hints(handoff.screening)
        self.assertEqual({tuple(sorted(s)) for s in hints["signals"]}, {("kind", "strength")})
        self.assertEqual({s["kind"] for s in hints["signals"]}, {"PROFANITY_VARIANT", "TARGETED_ATTACK"})


if __name__ == "__main__":
    unittest.main()
