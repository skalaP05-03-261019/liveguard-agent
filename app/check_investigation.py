"""Explicit real-model + real-MCP investigation check. No mutation or fixture fallback."""

import argparse
import asyncio
import json
from pathlib import Path

from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.investigation.graph import build_investigation_graph
from app.investigation.model import model_from_env
from app.schemas import TaskEnvelope


async def check(path):
    # Configuration must be supplied intentionally. Never print key values.
    model = model_from_env()
    business = BusinessMcpReadClient(BusinessMcpSettings.from_env())
    policy = RagMcpReadClient(RagMcpSettings.from_env())
    task = TaskEnvelope.model_validate_json(Path(path).read_text())
    if task.kind not in {"ANALYZE", "REANALYZE"}:
        raise ValueError("Analysis-only task required")
    graph = build_investigation_graph(model=model, business=business, policy=policy)
    result = await graph.ainvoke({"task": task})
    # Deliberately no raw chat, reasons, tool payloads, keys or internal reasoning.
    print(json.dumps({
        "status": "investigation_verified", "traceId": str(task.trace_id),
        "policyVersion": result["policy_version"], "modelCalls": result["turn_count"],
        "findings": [{"messageId": str(f.message_id), "verdict": f.verdict,
                      "categories": f.categories, "policyIds": f.policy_ids}
                     for f in result["message_findings"]],
        "mutationExecuted": False,
    }, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-json", required=True, help="Stored Backend analysis task envelope JSON")
    args = parser.parse_args()
    try:
        asyncio.run(check(args.task_json))
    except AgentToolError as error:
        print(json.dumps({"status": "failed", "errorCode": error.code,
                          "retryable": error.retryable, "traceId": error.trace_id}))
        raise SystemExit(1) from None
    except (ValueError, OSError):
        print(json.dumps({"status": "not_configured", "errorCode": "INVALID_INVESTIGATION_CONFIGURATION"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
