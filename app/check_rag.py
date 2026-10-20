"""Explicit real-server consumer check; never substitutes fixture evidence."""

import argparse
import asyncio
import hashlib
import json
from uuid import uuid4

from app.clients.business_mcp import AgentToolError
from app.policy_evidence import retrieve_policy_evidence


async def check(args):
    result = await retrieve_policy_evidence(
        {"trace_id": str(uuid4()), "policy_version": args.policy_version},
        query=args.query, top_k=args.top_k,
    )
    return {
        "status": "verified_against_configured_rag_server",
        "policyVersion": args.policy_version,
        "evidence": [{
            "policyId": section["policyId"], "version": section["version"],
            "resourceUri": section["resourceUri"],
            "textSha256": hashlib.sha256(section["text"].encode()).hexdigest(),
        } for section in result["policy_evidence"]],
        "limits": "Connectivity/provenance only; not retrieval quality, model judgment or sanctions.",
    }


def main():
    parser = argparse.ArgumentParser(description="Verify search→section→Resource against the configured RAG MCP.")
    parser.add_argument("--policy-version", required=True, help="Exact provider-confirmed wire version, no default")
    parser.add_argument("--query", required=True, help="Non-sensitive policy lookup query")
    parser.add_argument("--top-k", type=int, default=1, choices=range(1, 5))
    args = parser.parse_args()
    try:
        result = asyncio.run(check(args))
    except AgentToolError as error:
        print(json.dumps({"status": "failed", "errorCode": error.code,
                          "traceId": error.trace_id, "retryable": error.retryable}))
        return 1
    except ValueError:
        print(json.dumps({"status": "not_configured", "errorCode": "INVALID_RAG_CONFIGURATION"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
