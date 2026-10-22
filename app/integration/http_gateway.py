"""Trusted BE gateway. No decisions from the model or browser resume body."""
from contextlib import asynccontextmanager
import os
from urllib.parse import urlsplit
from uuid import UUID
import httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from app.clients.business_mcp import AgentToolError, BusinessMcpSettings, _decode_result
from app.integration.judgment_http import JudgmentHttpClient
from app.business_mcp.schemas import KickCommand
from app.integration.backend_events import BackendEvents
from app.runtime.fencing import check_execution

class HttpModerationGateway:
    def __init__(self, client, *, service_token, business_settings):
        self.client, self._token, self.business = client, service_token, business_settings
        self.judgments = JudgmentHttpClient(client, service_token=service_token)
        self.events = BackendEvents(client,service_token=service_token)

    async def progress(self, *, case_id, trace_id, event, payload):
        await self.events.publish(case_id=case_id,trace_id=trace_id,event=event,payload=payload)

    def __repr__(self):
        return "HttpModerationGateway(service_token=<redacted>)"

    async def _request(self, method, path, trace_id, body=None):
        await check_execution()
        try:
            response = await self.client.request(method, path, json=body,
                headers={"X-Service-Token": self._token, "X-Trace-Id": str(UUID(trace_id))},
                follow_redirects=False)
        except httpx.RequestError:
            raise AgentToolError("BACKEND_DELIVERY_UNKNOWN", trace_id=trace_id) from None
        if response.status_code != 200:
            code = {400:"VALIDATION_FAILED",403:"FORBIDDEN",404:"NOT_FOUND",
                    409:"STALE_VERSION",422:"REVIEW_NOT_APPROVED",501:"NOT_IMPLEMENTED"}.get(
                    response.status_code,"BACKEND_REJECTED")
            raise AgentToolError(code, trace_id=trace_id)
        try:
            envelope = response.json()
            if envelope.get("traceId") != trace_id or not isinstance(envelope.get("data"),dict):
                raise ValueError
            return envelope["data"]
        except (ValueError,AttributeError):
            raise AgentToolError("INVALID_BACKEND_RESULT", trace_id=trace_id) from None

    async def read_case(self, *, case_id, trace_id):
        return await self._request("GET", f"/internal/cases/{UUID(case_id)}", trace_id)

    async def read_action(self, *, action_id, trace_id):
        from app.clients.business_mcp import BusinessMcpReadClient
        result = await BusinessMcpReadClient(self.business).call(
            "get_action_status", {"actionId": str(UUID(action_id)), "traceId": trace_id})
        return result["data"]

    async def create_review(self, *, card, trace_id):
        # A-9: judgment creates the review; no separate review mutation.
        analysis = {k:card[k] for k in ("caseId","liveId","snapshotId","evidenceVersion","policyVersion","handlingRoute")}
        analysis["messageFindings"] = card["findings"]
        receipt = await self.judgments.submit(analysis=analysis,trace_id=trace_id)
        return receipt

    async def get_decision(self, *, review_id, decision_id, trace_id):
        return await self._request("GET",f"/internal/reviews/{UUID(review_id)}/decisions/{UUID(decision_id)}",trace_id)

    async def apply_context(self, *, request, trace_id):
        return await self._request("POST",f"/internal/reviews/{UUID(request['reviewId'])}/apply",trace_id,request)

    async def submit_analysis(self, *, analysis, trace_id):
        receipt = await self.judgments.submit(analysis=analysis,trace_id=trace_id)
        if receipt["reviewKind"] == "CONTEXT":
            return receipt
        current = await self._request("GET",f"/internal/cases/{UUID(analysis['caseId'])}",trace_id)
        refs = {k:current[k] for k in ("caseId","liveId","snapshotId","evidenceVersion","policyVersion")}
        if any(refs[k] != analysis[k] for k in refs):
            raise AgentToolError("STALE_EVIDENCE",trace_id=trace_id)
        expected = receipt["confirmationStatus"]
        if current.get("confirmationStatus") != expected or expected not in {"CONFIRMED_NORMAL","CONFIRMED_VIOLATION"}:
            raise AgentToolError("INVALID_BACKEND_RESULT",trace_id=trace_id)
        return {**refs,"observation":"CONFIRMED","status":"NO_ACTION" if expected=="CONFIRMED_NORMAL" else "APPLIED",
                "kickReview":current.get("kickReview",{"required":False})}

    async def revalidate(self, *, review_id, decision_id, trace_id):
        return await self._request("POST",f"/internal/kick-reviews/{UUID(review_id)}/revalidate",
                                   trace_id,{"decisionId":str(UUID(decision_id))})

    async def kick(self, action_command, *, trace_id):
        # Code-owned, post-interrupt only. Never expose mutation tools to the model.
        command = KickCommand.model_validate(action_command)
        async with create_mcp_http_client(headers={"Authorization":f"Bearer {self.business.token}"}) as http:
            async with Client(streamable_http_client(self.business.url,http_client=http),
                              read_timeout_seconds=self.business.timeout_seconds) as client:
                await check_execution()
                result = await client.call_tool("kick_user",{
                    "actionCommand":command.model_dump(mode="json",by_alias=True,exclude_none=True),
                    "traceId":trace_id})
                try:
                    return _decode_result(result,trace_id)["data"]
                except AgentToolError as error:
                    if error.code == "ACTION_OUTCOME_UNKNOWN":
                        return {"actionId":str(command.action_id),"status":"UNKNOWN"}
                    raise

@asynccontextmanager
async def configured_gateway():
    url = os.environ["BACKEND_BASE_URL"]
    token = os.environ["BACKEND_SERVICE_TOKEN"]
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http","https"} or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"","/"} or not token.strip()):
        raise ValueError("Explicit Backend origin and service credential required")
    async with httpx.AsyncClient(base_url=url,timeout=10,follow_redirects=False) as client:
        yield HttpModerationGateway(client,service_token=token,business_settings=BusinessMcpSettings.from_env())
