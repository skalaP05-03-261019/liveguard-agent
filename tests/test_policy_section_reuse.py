"""Regression for repeated policy reads consuming the shared Harness budget."""

from copy import deepcopy
import unittest

from app.case_loader import load_case_data
from app.harness.execution import HarnessGate
from app.investigation.model import ToolCall
from app.investigation.runner import run_investigation
from app.schemas import TaskEnvelope
from test_agent_read import FIXTURE, FixtureTools
from test_investigation import Policies, ScriptedModel, finding, finish, search


class PolicySectionReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.business = FixtureTools(deepcopy(FIXTURE))
        self.state = await load_case_data(TaskEnvelope.model_validate(FIXTURE["task"]),
                                          self.business)
        self.policy = Policies()

    async def test_direct_repeats_after_search_do_not_spend_section_budget(self):
        model = ScriptedModel([
            search(1),
            ToolCall("section-1", "get_policy_section", {"policyId": "CP-2.1"}),
            ToolCall("section-2", "get_policy_section", {"policyId": "CP-2.1"}),
            ToolCall("section-3", "get_policy_section", {"policyId": "CP-2.1"}),
            finish([finding(self.state)], n=5),
        ])
        gate = HarnessGate(trace_id=self.state["trace_id"])
        result = await run_investigation(self.state, model=model, business=self.business,
                                         policy=self.policy, gate=gate)
        self.assertEqual(result["message_findings"][0].policy_ids, ["CP-2.1"])
        self.assertEqual([name for name, _ in self.policy.calls],
                         ["search_policy", "get_policy_section", "resource"])
        self.assertEqual(gate.snapshot()["toolCalls"]["get_policy_section"], 1)

    async def test_a_new_search_still_rechecks_policy_source(self):
        model = ScriptedModel([search(1), search(2),
                               finish([finding(self.state)], n=3)])
        gate = HarnessGate(trace_id=self.state["trace_id"])
        await run_investigation(self.state, model=model, business=self.business,
                                policy=self.policy, gate=gate)
        self.assertEqual([name for name, _ in self.policy.calls],
                         ["search_policy", "get_policy_section", "resource",
                          "search_policy", "get_policy_section", "resource"])
        self.assertEqual(gate.snapshot()["toolCalls"]["get_policy_section"], 2)
