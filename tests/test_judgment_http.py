"""Pinned to BE 882974d OpenAPI v1 A-9/A-16; controlled HTTP, not live BE proof."""
from copy import deepcopy
import json
import unittest

import httpx

from app.clients.business_mcp import AgentToolError
from app.integration.judgment_http import JudgmentHttpClient, judgment_body
from test_workflow_integration import LIVE, REVIEW, DECISION


def analysis(route="PII_INVESTIGATION", verdict="NEEDS_REVIEW"):
    return {"caseId": REVIEW, "liveId": LIVE, "handlingRoute": route, "snapshotId": DECISION,
            "evidenceVersion": 1, "policyVersion": "v3.1", "taskId": DECISION,
            "messageFindings": [{"messageId": REVIEW, "verdict": verdict,
                "categories": ["FLOOD"] if verdict == "VIOLATION" else [],
                "policyIds": ["CP-4.1"], "evidenceMessageIds": [REVIEW, DECISION],
                "reason": "동일 사용자의 반복 메시지를 확인함. 연락처 010-1234-5678",
                "missingFacts": ["소유자 확인 필요"] if verdict == "NEEDS_REVIEW" else []}]}


class JudgmentHttpTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, payload, response=None, status=200, failure=None):
        self.requests = []
        def handler(request):
            self.requests.append(request)
            if failure:
                raise failure("synthetic", request=request)
            return httpx.Response(status, json={"data": response or {
                "confirmationStatus": "PENDING", "reviewId": REVIEW, "reviewKind": "CONTEXT"},
                "traceId": DECISION})
        async with httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(handler)) as client:
            gateway = JudgmentHttpClient(client, service_token="test-secret")
            self.assertNotIn("test-secret", repr(gateway))
            return await gateway.submit(analysis=payload, trace_id=DECISION)

    async def test_wire_matches_v1_and_retains_flood_reason_without_extra_fields(self):
        original = analysis("CONTEXT_REVIEW", "VIOLATION")
        before = deepcopy(original)
        receipt = await self.invoke(original)
        request = self.requests[0]
        self.assertEqual(request.url.path, f"/internal/cases/{REVIEW}/judgment")
        self.assertEqual(request.headers["X-Service-Token"], "test-secret")
        self.assertEqual(request.headers["X-Trace-Id"], DECISION)
        self.assertNotIn("authorization", request.headers)
        body = json.loads(request.content)
        self.assertEqual(set(body), {"caseId", "liveId", "handlingRoute", "snapshotId",
                                    "evidenceVersion", "policyVersion", "messageFindings"})
        finding = body["messageFindings"][0]
        self.assertEqual(set(finding), {"messageId", "verdict", "categories", "policyIds", "reason", "missingFacts"})
        self.assertEqual(finding["categories"], ["FLOOD"])
        self.assertIn(REVIEW, finding["reason"])
        self.assertIn(DECISION, finding["reason"])
        self.assertNotIn("010-1234-5678", finding["reason"])
        self.assertEqual(original, before)
        self.assertEqual(receipt["reviewId"], REVIEW)

    async def test_pii_known_verdicts_accept_no_review(self):
        for verdict, confirmation in (("NO_VIOLATION", "CONFIRMED_NORMAL"),
                                      ("VIOLATION", "CONFIRMED_VIOLATION")):
            receipt = await self.invoke(analysis(verdict=verdict), {
                "confirmationStatus": confirmation, "reviewId": None, "reviewKind": None})
            self.assertIsNone(receipt["reviewId"])

    async def test_pending_requires_context_and_consistent_review_fields(self):
        for receipt in (
            {"confirmationStatus": "PENDING", "reviewId": None, "reviewKind": None},
            {"confirmationStatus": "PENDING", "reviewId": REVIEW, "reviewKind": "KICK"},
            {"confirmationStatus": "PENDING", "reviewId": "bad", "reviewKind": "CONTEXT"},
            {"confirmationStatus": "CONFIRMED_NORMAL", "reviewId": REVIEW, "reviewKind": "CONTEXT"},
            {"confirmationStatus": "PENDING", "reviewId": REVIEW, "reviewKind": None}):
            with self.assertRaises(AgentToolError) as raised:
                await self.invoke(analysis(), receipt)
            self.assertEqual(raised.exception.code, "INVALID_BACKEND_RESULT")

    async def test_legacy_policy_is_rejected_without_silent_relabeling(self):
        payload = analysis()
        payload["policyVersion"] = "3.1"
        with self.assertRaises(AgentToolError) as raised:
            await self.invoke(payload)
        self.assertEqual(raised.exception.code, "INVALID_INPUT")
        self.assertEqual(self.requests, [])
        self.assertEqual(payload["policyVersion"], "3.1")

    async def test_lost_response_never_retries_judgment(self):
        with self.assertRaises(AgentToolError) as raised:
            await self.invoke(analysis(), failure=httpx.ReadTimeout)
        self.assertEqual(raised.exception.code, "JUDGMENT_DELIVERY_UNKNOWN")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(self.requests), 1)

    async def test_unimplemented_stale_and_redirect_fail_closed(self):
        for status, code in ((501, "NOT_IMPLEMENTED"), (409, "STALE_VERSION"),
                             (403, "FORBIDDEN"), (307, "JUDGMENT_REJECTED")):
            with self.assertRaises(AgentToolError) as raised:
                await self.invoke(analysis(), status=status)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(len(self.requests), 1)

    async def test_trace_mismatch_and_non_json_are_not_accepted(self):
        for response in (httpx.Response(200, json={"data": {}, "traceId": LIVE}),
                         httpx.Response(200, text="invalid")):
            async with httpx.AsyncClient(base_url="http://backend", transport=httpx.MockTransport(lambda _: response)) as client:
                with self.assertRaises(AgentToolError) as raised:
                    await JudgmentHttpClient(client, service_token="test").submit(analysis=analysis(), trace_id=DECISION)
                self.assertEqual(raised.exception.code, "INVALID_BACKEND_RESULT")

    def test_projection_does_not_allow_model_generated_route_or_action(self):
        payload = analysis()
        payload["handlingRoute"] = "RULE_CONFIRMED"
        with self.assertRaises(ValueError):
            judgment_body(payload)

