"""OpenAPI v1 A-9/A-16 judgment transport, not a complete ModerationGateway.

The endpoint is planned in BE 882974d. This adapter never enables task readiness,
invents decision/apply routes, or treats judgment acceptance as action completion.
"""

from typing import Literal
from uuid import UUID

import httpx
from pydantic import ValidationError, model_validator

from app.clients.business_mcp import AgentToolError
from app.harness.safety import safe_presentation
from app.schemas import AnalysisResult, Contract
from app.runtime.fencing import check_execution


class JudgmentReceipt(Contract):
    confirmation_status: Literal["PENDING", "CONFIRMED_VIOLATION", "CONFIRMED_NORMAL", "CANCELLED"]
    review_id: UUID | None
    review_kind: Literal["CONTEXT", "KICK"] | None

    @model_validator(mode="after")
    def validate_review(self):
        if (self.review_id is None) != (self.review_kind is None):
            raise ValueError("reviewId and reviewKind must be present together")
        if self.review_kind == "CONTEXT" and self.confirmation_status != "PENDING":
            raise ValueError("CONTEXT review must remain pending")
        if self.review_kind == "KICK" and self.confirmation_status != "CONFIRMED_VIOLATION":
            raise ValueError("KICK review requires a confirmed violation")
        return self


def judgment_body(analysis: dict) -> dict:
    """Project internal evidence-rich findings onto the additionalProperties:false wire.

    Evidence UUIDs stay in Graph/checkpoints and are described in reason as agreed
    with B1. taskId/threadId are not new fields in the BE contract. Never relabel
    a snapshot as another policy version to bypass the version check.
    """
    result = AnalysisResult.model_validate({k: analysis[k] for k in (
        "caseId", "snapshotId", "evidenceVersion", "policyVersion", "messageFindings")})
    live_id = str(UUID(str(analysis["liveId"])))
    route = analysis["handlingRoute"]
    if route not in {"CONTEXT_REVIEW", "PII_INVESTIGATION"} or result.policy_version not in {"v3.1", "v3.2"}:
        raise ValueError("AI judgment requires an AI route and a supported exact policy version")
    body = result.model_dump(mode="json", by_alias=True)
    for finding in body["messageFindings"]:
        evidence = finding.pop("evidenceMessageIds")
        finding["reason"] = safe_presentation(finding["reason"])
        if evidence:
            finding["reason"] += "\n판단 근거 메시지 ID: " + ", ".join(evidence)
        finding["missingFacts"] = safe_presentation(finding["missingFacts"])
    return {**body, "liveId": live_id, "handlingRoute": route}


class JudgmentHttpClient:
    """Single authenticated submission. Uncertain delivery is never auto-retried."""

    def __init__(self, client: httpx.AsyncClient, *, service_token: str):
        if not service_token.strip() or any(c in service_token for c in "\r\n"):
            raise ValueError("Backend service token is required")
        self._client, self._token = client, service_token

    def __repr__(self):
        return "JudgmentHttpClient(service_token=<redacted>)"

    async def submit(self, *, analysis: dict, trace_id: str) -> dict:
        try:
            trace_id = str(UUID(trace_id))
            body = judgment_body(analysis)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise AgentToolError("INVALID_INPUT", trace_id=trace_id) from None
        await check_execution()
        try:
            response = await self._client.post(
                f"/internal/cases/{body['caseId']}/judgment", json=body,
                headers={"X-Service-Token": self._token, "X-Trace-Id": trace_id},
                follow_redirects=False)
        except httpx.RequestError:
            raise AgentToolError("JUDGMENT_DELIVERY_UNKNOWN", trace_id=trace_id) from None
        if response.status_code != 200:
            code = {400: "VALIDATION_FAILED", 401: "UNAUTHENTICATED", 403: "FORBIDDEN",
                    404: "NOT_FOUND", 409: "STALE_VERSION", 501: "NOT_IMPLEMENTED"}.get(
                response.status_code, "JUDGMENT_REJECTED")
            raise AgentToolError(code, trace_id=trace_id)
        try:
            envelope = response.json()
            if not isinstance(envelope, dict) or envelope.get("traceId") != trace_id:
                raise ValueError("Invalid trace envelope")
            receipt = JudgmentReceipt.model_validate(envelope["data"])
            needs_context = body["handlingRoute"] == "CONTEXT_REVIEW" or any(
                f["verdict"] == "NEEDS_REVIEW" for f in body["messageFindings"])
            if needs_context and (receipt.confirmation_status != "PENDING" or receipt.review_kind != "CONTEXT"):
                raise ValueError("Missing required context review")
            if not needs_context and receipt.review_kind == "CONTEXT":
                raise ValueError("Unexpected mandatory approval")
        except (ValueError, TypeError, KeyError, ValidationError):
            raise AgentToolError("INVALID_BACKEND_RESULT", trace_id=trace_id) from None
        return receipt.model_dump(mode="json", by_alias=True)
