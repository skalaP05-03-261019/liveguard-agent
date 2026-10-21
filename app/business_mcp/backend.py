import re
from typing import Any
from uuid import UUID

import httpx

from app.business_mcp.config import Settings
from app.business_mcp.schemas import ActionCommand, BackendError


class BackendApi:
    """Narrow adapter for the proposed authenticated Spring /internal API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    async def get_case_context(
        self,
        case_id: UUID,
        *,
        view: str,
        trace_id: str,
        params: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        paths = {"moderationCase": "", "chatContext": "/context",
                 "streamInfo": "/channel-info", "officialLinks": "/channel-info",
                 "securityFacts": "/security-facts"}
        if view not in paths or (params and view != "chatContext"):
            raise BackendError("INVALID_INPUT", "Unsupported context view or parameters",
                               retryable=False, trace_id=trace_id)
        return await self._request(
            "GET",
            f"/internal/cases/{case_id}{paths[view]}",
            trace_id=trace_id,
            params=params,
        )

    async def get_user_history(
        self, case_id: UUID, *, days: int | None, trace_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/internal/cases/{case_id}/user-history",
            trace_id=trace_id,
            params={"days": days} if days is not None else None,
        )

    async def get_action_status(
        self, action_id: UUID, *, trace_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "GET", f"/internal/actions/{action_id}", trace_id=trace_id
        )

    async def execute_action(
        self, command: ActionCommand, *, trace_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/internal/actions/{command.action_id}/execute",
            trace_id=trace_id,
            json=command.model_dump(mode="json", by_alias=True),
        )

    async def reconcile_review(
        self,
        case_id: UUID,
        decision_id: UUID,
        *,
        trace_id: str,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/internal/cases/{case_id}/reviews/{decision_id}/reconcile",
            trace_id=trace_id,
            json={"caseId": str(case_id), "decisionId": str(decision_id)},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        trace_id: str,
        params: dict[str, int | str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._settings.backend_service_token:
            raise BackendError(
                "UNAUTHENTICATED",
                "Business MCP backend service credentials are not configured",
                retryable=False,
                trace_id=trace_id,
            )

        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers={
                    "X-Service-Token": self._settings.backend_service_token,
                    "X-Trace-Id": trace_id,
                },
            )
        except httpx.RequestError as error:
            raise BackendError(
                "TEMPORARY_UNAVAILABLE",
                "Backend request could not be completed",
                retryable=True,
                trace_id=trace_id,
            ) from error

        if not response.is_success:
            self._raise_response_error(response, trace_id)

        try:
            result = response.json()
        except ValueError as error:
            raise BackendError(
                "TEMPORARY_UNAVAILABLE",
                "Backend returned an invalid JSON response",
                retryable=True,
                trace_id=trace_id,
            ) from error

        if not isinstance(result, dict):
            raise BackendError(
                "TEMPORARY_UNAVAILABLE",
                "Backend response must be a JSON object",
                retryable=True,
                trace_id=trace_id,
            )
        data = result.get("data", result)
        if not isinstance(data, dict):
            raise BackendError(
                "TEMPORARY_UNAVAILABLE",
                "Backend data must be a JSON object",
                retryable=True,
                trace_id=trace_id,
            )
        return data

    @staticmethod
    def _raise_response_error(response: httpx.Response, trace_id: str) -> None:
        code = "TEMPORARY_UNAVAILABLE" if response.status_code >= 500 else "FORBIDDEN"
        message = "Backend rejected the request"
        retryable = response.status_code >= 500

        if response.status_code == 401:
            code = "UNAUTHENTICATED"
        elif response.status_code == 403:
            code = "FORBIDDEN"
        elif response.status_code == 404:
            code = "NOT_FOUND"
        elif response.status_code == 400 and code == "FORBIDDEN":
            code = "INVALID_EVIDENCE"
        elif response.status_code == 422 and code == "FORBIDDEN":
            code = "OUT_OF_POLICY"
        elif response.status_code == 409 and code == "FORBIDDEN":
            code = "STALE_EVIDENCE"
        elif response.status_code == 429:
            code = "TEMPORARY_UNAVAILABLE"
            retryable = True

        # HTTP 상태는 기본값만 제공하고, 유효한 BE 업무 오류 코드를 우선한다.
        try:
            body = response.json()
        except ValueError:
            body = None

        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                candidate_code = error.get("code")
                candidate_retryable = error.get("retryable")
                if (
                    isinstance(candidate_code, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", candidate_code)
                ):
                    code = candidate_code
                if isinstance(candidate_retryable, bool):
                    retryable = candidate_retryable

        raise BackendError(code, message, retryable=retryable, trace_id=trace_id)
