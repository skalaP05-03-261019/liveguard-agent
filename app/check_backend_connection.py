"""Inspect actual Spring/MCP connectivity without inventing missing contract fields.

Uses BUSINESS_BACKEND_BASE_URL, BUSINESS_MCP_SERVICE_TOKEN, BUSINESS_MCP_URL,
BUSINESS_MCP_AGENT_TOKEN. Never prints credentials or raw chat. Optional test event
must be explicitly enabled and should use a disposable verification case.
"""

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError

from app.case_loader import CaseSnapshot, ChatContext, load_case_data
from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings, AgentToolError
from app.integration.backend_events import BackendEvents
from app.schemas import TaskEnvelope


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
              "readContractReady": False, "blockers": [],
              "notice": "Connectivity inspection, not moderation/approval/kick execution proof"}
    checks = report["checks"]
    headers = {"X-Service-Token": token, "X-Trace-Id": trace_id}
    try:
        async with httpx.AsyncClient(base_url=base, timeout=10) as client:
            path = f"/internal/cases/{case_id}/context"
            checks["withoutServiceToken"] = (await client.get(path)).status_code
            checks["bearerOnly"] = (await client.get(path, headers={"Authorization": f"Bearer {token}"})).status_code
            report["backendHttpObserved"] = True
            checks["authBoundaryReady"] = all(checks[key] in {401, 403}
                                                 for key in ("withoutServiceToken", "bearerOnly"))
            if not checks["authBoundaryReady"]:
                report["blockers"].append({"stage": "serviceAuth", "errorCode": "AUTH_BOUNDARY_NOT_VERIFIED"})

            async def http_probe(name, suffix):
                try:
                    response = await client.get(f"/internal/cases/{case_id}{suffix}", headers=headers,
                                                params={"before": 0, "after": 0} if name == "context" else None)
                    observed = {"httpStatus": response.status_code}
                    checks.setdefault("httpReads", {})[name] = observed
                    if response.status_code != 200:
                        report["blockers"].append({"stage": name, "httpStatus": response.status_code})
                        return
                    payload = response.json()
                    data = payload.get("data") if isinstance(payload, dict) else None
                    if not isinstance(data, dict):
                        raise ValueError("Invalid envelope")
                    if name == "context":
                        checks["contextMessageCount"] = len(data.get("messages", []))
                        checks["rawContextSchemaGaps"] = missing(ChatContext, data)
                    elif name == "case":
                        checks["rawCaseSchemaGaps"] = missing(CaseSnapshot, data)
                except (httpx.RequestError, ValueError, TypeError, AttributeError):
                    checks.setdefault("httpReads", {})[name] = {"errorCode": "BACKEND_INSPECTION_FAILED"}
                    report["blockers"].append({"stage": name, "errorCode": "BACKEND_INSPECTION_FAILED"})

            await asyncio.gather(*(http_probe(name, suffix) for name, suffix in (
                ("context", "/context"), ("case", ""), ("channelInfo", "/channel-info"),
                ("userHistory", "/user-history"), ("securityFacts", "/security-facts"),
                ("kickEligibility", "/kick-eligibility"))))
            checks["contextWithServiceToken"] = checks["httpReads"]["context"].get("httpStatus")
            checks["caseEndpoint"] = checks["httpReads"]["case"].get("httpStatus")
            checks["channelInfo"] = checks["httpReads"]["channelInfo"].get("httpStatus")
            docs = await client.get("/v3/api-docs")
            if docs.status_code == 200:
                paths = docs.json().get("paths", {})
                # OpenAPI lists 501 stubs too. Documentation is not execution proof.
                normalized = {re.sub(r"\{[^/{}]+\}", "{}", path) for path in paths}
                checks["documentedB2Routes"] = {
                    name: re.sub(r"\{[^/{}]+\}", "{}", path) in normalized for name, path in {
                        "case": "/internal/cases/{caseId}",
                        "judgment": "/internal/cases/{caseId}/judgment",
                        "actionExecution": "/internal/actions/{actionId}/execute",
                        "contextDecision": "/api/context-reviews/{reviewId}/decision",
                        "kickDecision": "/api/kick-reviews/{reviewId}/decision",
                    }.items()}
            if publish_test_event and checks["contextWithServiceToken"] == 200:
                await BackendEvents(client, service_token=token).publish(
                    case_id=case_id, trace_id=trace_id, event="TOOL_END",
                    payload={"tool": "connection_probe", "callId": str(uuid4()), "status": "ok", "elapsedMs": 0})
                checks["testProgressEvent"] = "ACCEPTED_202"
    except (httpx.RequestError, ValueError, TypeError, AttributeError):
        report["blockers"].append({"stage": "http", "errorCode": "BACKEND_INSPECTION_FAILED"})

    payloads = {}
    try:
        business = BusinessMcpReadClient(BusinessMcpSettings.from_env())
        async def mcp_probe(name):
            try:
                args = {"caseId": case_id, "traceId": trace_id}
                if name == "get_chat_context":
                    args.update(before=0, after=0)
                payload = await business.call(name, args)
                if not isinstance(payload.get("data"), dict):
                    raise ValueError("Invalid envelope")
                payloads[name] = payload
                checks.setdefault("mcpReads", {})[name] = {"status": "CONNECTED"}
            except AgentToolError as error:
                checks.setdefault("mcpReads", {})[name] = {"errorCode": error.code, "retryable": error.retryable}
                report["blockers"].append({"stage": name, "errorCode": error.code})
            except (ValueError, TypeError, AttributeError):
                checks.setdefault("mcpReads", {})[name] = {"errorCode": "INVALID_TOOL_RESPONSE"}
                report["blockers"].append({"stage": name, "errorCode": "INVALID_TOOL_RESPONSE"})
        await asyncio.gather(*(mcp_probe(name) for name in (
            "get_chat_context", "get_moderation_case", "get_stream_info", "get_official_links",
            "get_security_facts", "get_user_history")))
        context = payloads.get("get_chat_context")
        case = payloads.get("get_moderation_case")
        if context:
            checks["mcpToSpringContext"] = "CONNECTED"
            checks["mcpContextSchemaGaps"] = missing(ChatContext, context["data"])
        if case:
            checks["mcpCaseSchemaGaps"] = missing(CaseSnapshot, case["data"])
            checks["workflowScopeMissing"] = [key for key in ("liveId", "userId", "handlingRoute")
                                              if not case["data"].get(key)]
        if context and case:
            gaps = (checks["mcpContextSchemaGaps"] or checks["mcpCaseSchemaGaps"]
                    or checks["workflowScopeMissing"])
            if gaps:
                report["blockers"].append({"stage": "readSchema", "errorCode": "BACKEND_CONTRACT_INCOMPLETE"})
            else:
                # Reuse the real loader's case/snapshot/version/target binding checks.
                task = TaskEnvelope(event_id=uuid4(), task_id=uuid4(), case_id=case_id, kind="ANALYZE",
                                    thread_id=f"probe:{trace_id}", snapshot_id=case["data"]["snapshotId"],
                                    expected_evidence_version=case["data"]["evidenceVersion"], trace_id=trace_id)
                class RecordedReads:
                    async def call(self, name, arguments):
                        return payloads[name]
                try:
                    await load_case_data(task, RecordedReads())
                    checks["evidenceBindingReady"] = True
                except AgentToolError as error:
                    checks["evidenceBindingReady"] = False
                    report["blockers"].append({"stage": "evidenceBinding", "errorCode": error.code})
                report["readContractReady"] = bool(checks.get("evidenceBindingReady")
                                                    and checks.get("authBoundaryReady")
                                                    and checks.get("caseEndpoint") == 200
                                                    and checks.get("contextWithServiceToken") == 200)
    except (ValueError, TypeError, AttributeError):
        report["blockers"].append({"stage": "mcp", "errorCode": "MCP_NOT_CONFIGURED"})
    report["status"] = ("READ_CONTRACT_READY" if report["readContractReady"] else
                        "PARTIAL" if report["backendHttpObserved"] else "BLOCKED")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", type=UUID, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publish-test-event", action="store_true")
    parser.add_argument("--require-ready", action="store_true", help="Exit 2 unless read contract and auth are verified")
    args = parser.parse_args()
    report = asyncio.run(inspect_connection(str(args.case_id), publish_test_event=args.publish_test_event))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] == "BLOCKED" or (args.require_ready and not report.get("readContractReady")):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
