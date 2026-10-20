"""Inspect actual Spring/MCP connectivity without inventing missing contract fields.

Uses BUSINESS_BACKEND_BASE_URL, BUSINESS_MCP_SERVICE_TOKEN, BUSINESS_MCP_URL,
BUSINESS_MCP_AGENT_TOKEN. Never prints credentials or raw chat. Optional test event
must be explicitly enabled and should use a disposable verification case.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from app.case_loader import CaseSnapshot, ChatContext
from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings, AgentToolError
from app.integration.backend_events import BackendEvents


def missing(model, data):
    try:
        model.model_validate(data)
    except ValidationError as error:
        return [".".join(str(p) for p in item["loc"]) for item in error.errors()]
    return []


async def inspect_connection(case_id: str, *, publish_test_event=False):
    trace_id = str(uuid4())
    base = os.environ.get("BUSINESS_BACKEND_BASE_URL", "")
    token = os.environ.get("BUSINESS_MCP_SERVICE_TOKEN", "")
    if not base or not token:
        return {"status": "BLOCKED", "errorCode": "BACKEND_NOT_CONFIGURED"}
    report = {"status": "PARTIAL", "traceId": trace_id, "caseId": case_id,
              "backendHttpObserved": False, "mutationExecuted": False, "checks": {},
              "notice": "Connectivity inspection, not moderation/approval/kick execution proof"}
    checks = report["checks"]
    headers = {"X-Service-Token": token, "X-Trace-Id": trace_id}
    try:
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            path = f"/internal/cases/{case_id}/context"
            checks["withoutServiceToken"] = (await client.get(path)).status_code
            checks["bearerOnly"] = (await client.get(path, headers={"Authorization": f"Bearer {token}"})).status_code
            response = await client.get(path, params={"before": 0, "after": 0}, headers=headers)
            checks["contextWithServiceToken"] = response.status_code
            if response.status_code != 200:
                report.update(status="BLOCKED", errorCode="CONTEXT_UNAVAILABLE")
                return report
            report["backendHttpObserved"] = True
            data = response.json().get("data", {})
            checks["contextMessageCount"] = len(data.get("messages", []))
            checks["rawContextSchemaGaps"] = missing(ChatContext, data)
            checks["channelInfo"] = (await client.get(
                f"/internal/cases/{case_id}/channel-info", headers=headers)).status_code
            checks["caseEndpoint"] = (await client.get(f"/internal/cases/{case_id}", headers=headers)).status_code
            docs = await client.get("/v3/api-docs")
            if docs.status_code == 200:
                paths = docs.json().get("paths", {})
                checks["implementedB2Routes"] = {
                    name: path in paths for name, path in {
                        "case": "/internal/cases/{caseId}",
                        "judgment": "/internal/cases/{caseId}/judgment",
                        "kickExecution": "/internal/kick-reviews/{reviewId}/execute",
                        "contextDecision": "/api/context-reviews/{id}/decision",
                    }.items()}
            if publish_test_event:
                await BackendEvents(client, service_token=token).publish(
                    case_id=case_id, trace_id=trace_id, event="TOOL_END",
                    payload={"tool": "connection_probe", "callId": str(uuid4()), "status": "ok", "elapsedMs": 0})
                checks["testProgressEvent"] = "ACCEPTED_202"
        business = BusinessMcpReadClient(BusinessMcpSettings.from_env())
        context = await business.call("get_chat_context", {"caseId": case_id, "traceId": trace_id,
                                                         "before": 0, "after": 0})
        checks["mcpToSpringContext"] = "CONNECTED"
        checks["mcpContextSchemaGaps"] = missing(ChatContext, context["data"])
        case = await business.call("get_moderation_case", {"caseId": case_id, "traceId": trace_id})
        checks["mcpCaseSchemaGaps"] = missing(CaseSnapshot, case["data"])
        checks["workflowScopeMissing"] = [key for key in ("liveId", "userId", "handlingRoute")
                                          if not case["data"].get(key)]
        report["readContractReady"] = not (checks["mcpContextSchemaGaps"] or checks["mcpCaseSchemaGaps"]
                                           or checks["workflowScopeMissing"])
        report["status"] = "READ_CONTRACT_READY" if report["readContractReady"] else "PARTIAL"
    except AgentToolError as error:
        report.update(errorCode=error.code)
    except (httpx.RequestError, ValueError, TypeError, AttributeError):
        report.update(status="BLOCKED", errorCode="BACKEND_INSPECTION_FAILED")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", type=UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-test-event", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(inspect_connection(str(args.case_id), publish_test_event=args.publish_test_event))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] == "BLOCKED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
