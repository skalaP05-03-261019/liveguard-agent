"""The Phase 2 investigator must not combine contradictory policy instructions."""

import json
import unittest

from app.investigation_eval.cases import FixtureBusiness, build_fixture, build_handoff, load_case_set
from app.phase2_bridge.handoff import run_readonly_investigation
from app.phase2_investigation.investigator import Phase2Investigator, system_prompt
from test_investigation import Policies, ScriptedModel, finish
from test_investigation_eval import CASES


class Phase2PolicyPromptTests(unittest.TestCase):
    def test_group_hate_boundary_is_version_specific(self):
        old = system_prompt("v3.1")
        new = system_prompt("v3.2")
        self.assertIn("CP-6.1", old)
        self.assertIn("집단 혐오를 HARASSMENT로 바꿔 판정하지 마세요", old)
        self.assertNotIn("CP-3.3", old)
        self.assertIn("CP-3.3", new)
        self.assertIn("기존 HARASSMENT", new)
        self.assertNotIn("집단 혐오를 HARASSMENT로 바꿔 판정하지 마세요", new)

    def test_legacy_fixture_uses_old_addendum(self):
        self.assertEqual(system_prompt("fixture-policy-v1"),
                         system_prompt("v3.1"))


class Phase2PolicyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_v32_candidate_hints_reach_investigation_without_becoming_evidence(self):
        item = next(case for case in load_case_set(CASES).cases if case.id == "D-P2-17")
        handoff = build_handoff(item, "v3.2")
        target = handoff.agent_state()["target_messages"][0]["messageId"]
        finding = {"messageId": target, "verdict": "NEEDS_REVIEW", "categories": [],
                   "policyIds": [], "evidenceMessageIds": [target],
                   "reason": "대상 집단이 불명확해 확인 필요", "missingFacts": ["지칭 대상 집단"]}
        model = ScriptedModel([finish([finding])])
        investigator = Phase2Investigator(model=model, business=FixtureBusiness(build_fixture(item, "v3.2")),
                                          policy=Policies(), observer=lambda event: None)
        draft = await run_readonly_investigation(handoff, investigator)
        messages, _, _ = model.inputs[0]
        self.assertEqual(messages[0]["content"], system_prompt("v3.2"))
        self.assertIn("candidateHints", json.loads(messages[1]["content"]))
        self.assertEqual(draft.analysis.policy_version, "v3.2")
        self.assertEqual(draft.analysis.message_findings[0].verdict, "NEEDS_REVIEW")


if __name__ == "__main__":
    unittest.main()
