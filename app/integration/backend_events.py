"""B1 PR #9's implemented event endpoint; never forwards raw tool arguments.

Judgment, context-review creation/decision and action endpoints are not inferred
from this adapter. It sends status only and cannot accept tasks or execute actions.
"""

from uuid import UUID

import httpx

from app.clients.business_mcp import AgentToolError


class BackendEvents:
    def __init__(self, client: httpx.AsyncClient, *, service_token: str):
        if not service_token:
            raise ValueError("Backend service token is required")
        self.client, self._token = client, service_token

    def __repr__(self):
        return "BackendEvents(service_token=<redacted>)"

    async def publish(self, *, case_id: str, trace_id: str, event: str, payload: dict):
        try:
            UUID(case_id)
            UUID(trace_id)
            if event in {"TOOL_START", "TOOL_END"}:
                allowed, required = {"tool", "callId", "status", "elapsedMs"}, {"tool", "callId"}
            elif event == "TASK_FAILED":
                allowed, required = {"stage", "errorCode", "traceUrl"}, {"stage", "errorCode"}
            else:
                raise ValueError
            clean = {k: v for k, v in payload.items() if k in allowed}
            if any(not isinstance(clean.get(k), str) or not clean[k].strip() for k in required):
                raise ValueError
            if any(not isinstance(v, str) or len(v) > 200 for k, v in clean.items() if k != "elapsedMs"):
                raise ValueError
            if "elapsedMs" in clean and (type(clean["elapsedMs"]) is not int or clean["elapsedMs"] < 0):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AgentToolError("INVALID_INPUT", trace_id=trace_id) from None
        try:
            response = await self.client.post(
                f"/internal/cases/{case_id}/events", json={"event": event, "payload": clean},
                headers={"Authorization": f"Bearer {self._token}", "X-Trace-Id": trace_id})
        except httpx.RequestError:
            # A lost response may have published the event; don't blindly resend it.
            raise AgentToolError("EVENT_DELIVERY_UNKNOWN", trace_id=trace_id) from None
        if response.status_code != 202:
            code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND"}.get(
                response.status_code, "BACKEND_EVENT_REJECTED")
            raise AgentToolError(code, trace_id=trace_id)
