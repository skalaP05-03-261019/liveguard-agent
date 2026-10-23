"""Run investigation-quality evaluation. Live mode calls the configured model and RAG MCP.

Confirmation cases require an explicit --split confirmation so they are not used while
tuning. Outputs are created exclusively; the details file (model reasons) is private.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from pydantic import ValidationError

from app.clients.rag_mcp import RagMcpReadClient, RagMcpSettings
from app.investigation.model import model_from_env
from app.phase2_bridge.handoff import ExistingInvestigator
from .cases import load_case_set
from .evaluate import UsageMeter, evaluate, source_fingerprint

INVESTIGATORS = ("existing",)


def _write_exclusive(path: Path, payload, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--standard", required=True, help="Expectation standard name in the case set")
    parser.add_argument("--split", choices=("development", "confirmation", "all"), default="development")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Opt in to configured model/RAG MCP usage")
    mode.add_argument("--predictions", type=Path, help="JSON object mapping case IDs to target findings")
    parser.add_argument("--investigator", choices=INVESTIGATORS, default="existing")
    parser.add_argument("--policy-version", default=os.getenv("LIVEGUARD_POLICY_VERSION") or os.getenv("POLICY_VERSION"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--details-out", type=Path, help="Private local file with model reasons")
    args = parser.parse_args()
    if any(p and p.exists() for p in (args.out, args.details_out)):
        parser.error("Output already exists; choose a new result file")
    if args.limit is not None and not 1 <= args.limit <= 200:
        parser.error("Limit must be in 1..200")
    if args.live and args.limit is None:
        parser.error("Live mode requires an explicit --limit (1..200)")
    if not args.policy_version:
        parser.error("Set --policy-version or LIVEGUARD_POLICY_VERSION")
    try:
        case_set = load_case_set(args.cases)
        cases = [c for c in case_set.cases if args.split == "all" or c.split == args.split]
        if args.limit is not None:
            cases = cases[:args.limit]
        if not cases:
            raise ValueError("No cases selected")
        if args.live:
            meter = UsageMeter(model_from_env())
            policy = RagMcpReadClient(RagMcpSettings.from_env())
            factory = lambda business: ExistingInvestigator(model=meter, business=business, policy=policy,
                                                            observer=lambda event: None)
            report, details = asyncio.run(evaluate(case_set, cases, args.standard, policy_version=args.policy_version,
                                                   investigator_factory=factory, meter=meter,
                                                   timeout_seconds=args.timeout))
            report["execution"] = {"mode": "live-model", "investigator": args.investigator,
                                   "sourceModel": os.getenv("MODEL_NAME"), "policyVersion": args.policy_version,
                                   "split": args.split, "timeoutSeconds": args.timeout, **source_fingerprint()}
        else:
            predictions = json.loads(args.predictions.read_text())
            if not isinstance(predictions, dict):
                raise ValueError("Predictions must be a JSON object")
            report, details = asyncio.run(evaluate(case_set, cases, args.standard, policy_version=args.policy_version,
                                                   predictions=predictions))
            report["execution"] = {"mode": "offline-predictions", "investigatorExecuted": False,
                                   "split": args.split}
    except (ValueError, ValidationError, OSError, TypeError, KeyError, json.JSONDecodeError):
        parser.exit(2, "Invalid configuration or input; no report was written.\n")
    _write_exclusive(args.out, report)
    if args.details_out:
        _write_exclusive(args.details_out, details, mode=0o600)
    print(json.dumps({"total": report["total"], "assessed": report["assessed"], "errors": report["errors"],
                      "satisfied": report["satisfied"], "unsupportedAssertions": report["unsupportedAssertions"]}))
    return 1 if report["errors"] else 0


raise SystemExit(main())
