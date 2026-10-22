from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from app.integration.http_gateway import HttpModerationGateway
from app.clients.business_mcp import AgentToolError,BusinessMcpSettings
from app.business_mcp.schemas import KickCommand
from app.rag_mcp.config import IndexSettings
from test_judgment_http import analysis
from test_workflow_integration import LIVE,REVIEW,DECISION

class HttpGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, fn, handler):
        async with httpx.AsyncClient(base_url="http://backend",transport=httpx.MockTransport(handler)) as client:
            gateway=HttpModerationGateway(client,service_token="private-test-token",
                business_settings=BusinessMcpSettings("http://business:8002/mcp","test"))
            self.assertNotIn("private-test-token",repr(gateway))
            return await fn(gateway)

    async def test_context_card_submits_judgment_not_new_review_route(self):
        payload=analysis("CONTEXT_REVIEW","NO_VIOLATION")
        card={**payload,"findings":payload["messageFindings"]}
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(200,json={"traceId":DECISION,"data":{"confirmationStatus":"PENDING","reviewId":REVIEW,"reviewKind":"CONTEXT"}})
        receipt=await self.call(lambda g:g.create_review(card=card,trace_id=DECISION),handler)
        self.assertEqual(receipt["reviewId"],REVIEW)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].url.path,f"/internal/cases/{REVIEW}/judgment")

    async def test_pii_pending_returns_receipt_without_read_or_mutation(self):
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(200,json={"traceId":DECISION,"data":{"confirmationStatus":"PENDING","reviewId":REVIEW,"reviewKind":"CONTEXT"}})
        result=await self.call(lambda g:g.submit_analysis(analysis=analysis(),trace_id=DECISION),handler)
        self.assertEqual(result["reviewKind"],"CONTEXT")
        self.assertEqual(len(calls),1)

    async def test_pii_known_result_requires_current_matching_case_confirmation(self):
        payload=analysis(verdict="NO_VIOLATION")
        for change,expected in (({},"NO_ACTION"),({"liveId":REVIEW},"STALE_EVIDENCE"),({"confirmationStatus":"PENDING"},"INVALID_BACKEND_RESULT")):
            def handler(req):
                data={"confirmationStatus":"CONFIRMED_NORMAL","reviewId":None,"reviewKind":None}
                if req.method=="GET":
                    data={**{k:payload[k] for k in ("caseId","liveId","snapshotId","evidenceVersion","policyVersion")},
                          "confirmationStatus":"CONFIRMED_NORMAL",**change}
                return httpx.Response(200,json={"traceId":DECISION,"data":data})
            if not change:
                result=await self.call(lambda g:g.submit_analysis(analysis=payload,trace_id=DECISION),handler)
                self.assertEqual(result["status"],expected)
            else:
                with self.assertRaises(AgentToolError) as raised:
                    await self.call(lambda g:g.submit_analysis(analysis=payload,trace_id=DECISION),handler)
                self.assertEqual(raised.exception.code,expected)

    async def test_get_decision_uses_service_auth_and_immutable_ids(self):
        def handler(req):
            self.assertEqual(req.url.path,f"/internal/reviews/{REVIEW}/decisions/{DECISION}")
            self.assertEqual(req.headers["X-Service-Token"],"private-test-token")
            self.assertNotIn("authorization",req.headers)
            return httpx.Response(200,json={"traceId":DECISION,"data":{"reviewId":REVIEW}})
        await self.call(lambda g:g.get_decision(review_id=REVIEW,decision_id=DECISION,trace_id=DECISION),handler)

    async def test_apply_lost_response_does_not_retry(self):
        calls=[]
        def handler(req):
            calls.append(req);raise httpx.ReadTimeout("synthetic",request=req)
        with self.assertRaises(AgentToolError) as raised:
            await self.call(lambda g:g.apply_context(request={"reviewId":REVIEW},trace_id=DECISION),handler)
        self.assertEqual(raised.exception.code,"BACKEND_DELIVERY_UNKNOWN")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(calls),1)

    async def test_recovery_reads_existing_case_and_mcp_action_status_only(self):
        requests = []
        def handler(req):
            requests.append(req)
            return httpx.Response(200,json={"traceId":DECISION,"data":{"caseId":REVIEW}})
        with patch("app.clients.business_mcp.BusinessMcpReadClient.call", new_callable=AsyncMock) as read:
            read.return_value = {"data":{"actionId":REVIEW,"status":"APPLIED"}}
            async def observe(gateway):
                case = await gateway.read_case(case_id=REVIEW,trace_id=DECISION)
                action = await gateway.read_action(action_id=REVIEW,trace_id=DECISION)
                return case, action
            case, action = await self.call(observe,handler)
            read.assert_awaited_once_with("get_action_status",{"actionId":REVIEW,"traceId":DECISION})
        self.assertEqual((requests[0].method,requests[0].url.path),
                         ("GET",f"/internal/cases/{REVIEW}"))
        self.assertEqual((case["caseId"],action["actionId"]),(REVIEW,REVIEW))

    async def test_malformed_trace_and_http_errors_fail_closed(self):
        for response in (httpx.Response(200,json={"traceId":LIVE,"data":{}}),
                         httpx.Response(200,text="invalid"),httpx.Response(307),httpx.Response(501)):
            with self.assertRaises(AgentToolError):
                await self.call(lambda g:g.get_decision(review_id=REVIEW,decision_id=DECISION,trace_id=DECISION),
                                lambda _:response)

    def test_exact_shared_version_index_keeps_version_and_resolves_existing_source(self):
        with patch.dict("os.environ",{"LIVEGUARD_POLICY_VERSION":"v3.1","POLICY_VERSION":"","RAG_DB_URL":"test","OPENAI_API_KEY":"test"},clear=False):
            settings=IndexSettings.from_env()
            self.assertEqual(settings.policy_version,"v3.1")
            self.assertEqual(settings.policy_file.name,"policy-v3.1.md")
            self.assertTrue(settings.policy_file.exists())
        with patch.dict("os.environ",{"LIVEGUARD_POLICY_VERSION":"v3.1","POLICY_VERSION":"3.1"},clear=False):
            with self.assertRaises(ValueError):IndexSettings.from_env()
