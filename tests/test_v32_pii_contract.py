"""v3.2 PII contract stays separate from existing v3.1 cases (BE #84)."""

from pathlib import Path
import asyncio
import json
import unittest

from app.clients.business_mcp import AgentToolError
from app.investigation.runner import prompt_for_policy, run_investigation
from app.investigation.validation import validate_analysis


TARGET = "00000000-0000-4000-8000-000000000001"
CASE = "00000000-0000-4000-8000-000000000002"
SNAPSHOT = "00000000-0000-4000-8000-000000000003"


def section(version, policy_id, category):
    return {"policyId": policy_id, "title": category, "text": "검증된 정책 원문",
            "version": version, "category": category,
            "resourceUri": f"policy://{version}/operating-policy/{policy_id}"}


def state(version):
    return {"case_id": CASE, "snapshot_id": SNAPSHOT, "evidence_version": 1,
            "policy_version": version, "handling_route": "PII_INVESTIGATION",
            "target_messages": [{"messageId": TARGET}], "messages": [{"messageId": TARGET}],
            "policy_evidence": [section(version, "SP-3.1", "PII_EXPOSURE")]}


def payload(version, *, mixed=False):
    return {"caseId": CASE, "snapshotId": SNAPSHOT, "evidenceVersion": 1,
            "policyVersion": version, "messageFindings": [{
                "messageId": TARGET, "verdict": "VIOLATION",
                "categories": ["PII_EXPOSURE", "HARASSMENT"] if mixed else ["PII_EXPOSURE"],
                "policyIds": ["SP-3.1", "CP-3.2"] if mixed else ["SP-3.1"],
                "evidenceMessageIds": [TARGET], "reason": "형식과 노출 문맥을 확인함",
                "missingFacts": []}]}


class V32PiiContractTests(unittest.TestCase):
    def test_only_be_phone_tag_reaches_v32_pii_model_input(self):
        class CaptureModel:
            async def complete(self, messages, tools, *, trace_id):
                self.messages = messages
                raise RuntimeError("captured first input")

        for version, tags, expected in (("v3.2", ["PII_PHONE"], True),
                                        ("v3.2", ["PII_EMAIL"], False),
                                        ("v3.1", ["PII_PHONE"], None)):
            case = {**state(version), "trace_id": CASE,
                    "case_snapshot": {"matchingTags": tags},
                    "context_messages": []}
            model = CaptureModel()
            with self.subTest(version=version, tags=tags), self.assertRaises(RuntimeError):
                asyncio.run(run_investigation(case, model=model, business=None, policy=None))
            sent = json.loads(model.messages[1]["content"])
            if expected is None:
                self.assertNotIn("backendPiiPhoneCandidate", sent)
            else:
                self.assertIs(sent["backendPiiPhoneCandidate"], expected)

    def test_only_v32_prompt_accepts_unknown_for_phone_form(self):
        current = prompt_for_policy("v3.2")
        previous = prompt_for_policy("v3.1")
        self.assertIn("PII_PHONE", current)
        self.assertIn("UNKNOWN", current)
        self.assertIn("다른 유형이 함께 의심되면", current)
        self.assertNotIn("PII_PHONE", previous)
        policy = (Path(__file__).resolve().parents[1] / "app/rag_mcp/policies/policy-v3.2.md").read_text()
        self.assertIn("본인 번호라는 주장으로 예외를 만들지 않는다", policy)

    def test_v32_pii_only_can_be_confirmed(self):
        self.assertEqual(validate_analysis(state("v3.2"), payload("v3.2"))
                         .message_findings[0].categories, ["PII_EXPOSURE"])

    def test_v32_mixed_violation_fails_before_backend_422(self):
        with self.assertRaises(AgentToolError) as raised:
            validate_analysis(state("v3.2"), payload("v3.2", mixed=True))
        self.assertEqual(raised.exception.code, "INVALID_MODEL_OUTPUT")

    def test_v31_policy_and_mixed_case_unchanged(self):
        old = state("v3.1")
        old["policy_evidence"].append(section("v3.1", "CP-3.2", "HARASSMENT"))
        self.assertEqual(len(validate_analysis(old, payload("v3.1", mixed=True))
                             .message_findings[0].categories), 2)


if __name__ == "__main__":
    unittest.main()
