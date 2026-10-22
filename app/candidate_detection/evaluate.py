"""Reproducible candidate-only metrics. Reports omit chat text, evidence quotes and reasons."""

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import re
import time
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, ValidationError
from app.clients.business_mcp import AgentToolError
from app.investigation.model import model_from_env
from app.schemas import Contract
from .contracts import Assessment, EXPERIMENTS, RiskKind, ScreeningInput, select_candidate, validate_evidence
from .screen import CandidateScreen, PROMPT


class EvalCase(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    input: ScreeningInput
    expected_candidate: bool = Field(strict=True)
    expected_kinds: list[RiskKind] | None = None


def load_cases(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list) or not data or len(data) > 1000:
        raise ValueError("Expected 1..1000 cases")
    cases = [EvalCase.model_validate(row) for row in data]
    if len({c.id for c in cases}) != len(cases):
        raise ValueError("Duplicate case ID")
    for case in cases:
        if case.expected_kinds is not None and len(set(case.expected_kinds)) != len(case.expected_kinds):
            raise ValueError("Duplicate expected risk kind")
        if not case.expected_candidate and case.expected_kinds:
            raise ValueError("Negative case cannot expect risk signals")
    return cases


def fingerprint(cases):
    payload = [c.model_dump(mode="json", by_alias=True) for c in cases]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def ratio(n, d):
    return round(n / d, 4) if d else None


def confusion(pairs):
    tp = sum(y and p for y, p in pairs)
    fp = sum(not y and p for y, p in pairs)
    tn = sum(not y and not p for y, p in pairs)
    fn = sum(y and not p for y, p in pairs)
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "f1": ratio(2 * tp, 2 * tp + fp + fn), "normalCandidateRate": ratio(fp, fp + tn)}


def summarize(cases, results):
    if len(cases) != len(results) or [c.id for c in cases] != [r["id"] for r in results]:
        raise ValueError("Results must match cases exactly")
    valid = [(c, r) for c, r in zip(cases, results) if r["status"] == "ASSESSED"]
    coverage = ratio(len(valid), len(cases))
    reports = {}
    for experiment in EXPERIMENTS:
        pairs = [(c.expected_candidate, r["candidates"][experiment]) for c, r in valid]
        decided = confusion(pairs)
        reports[experiment] = {"assessedOnly": decided,
                              "candidateCount": sum(p for _, p in pairs),
                              "missedExpectedCandidatesIncludingErrors": decided["fn"] + sum(
                                  c.expected_candidate for c, r in zip(cases, results) if r["status"] != "ASSESSED")}
    labeled = [(c, r) for c, r in valid if c.expected_kinds is not None]
    kinds = {kind.value: confusion([(kind in c.expected_kinds, kind.value in r["riskKinds"])
                                   for c, r in labeled]) for kind in RiskKind}
    latencies = sorted(r["latencyMs"] for r in results)
    percentile = lambda p: latencies[max(0, math.ceil(len(latencies) * p) - 1)] if latencies else None
    return {"kind": "candidate-screening-only", "datasetSha256": fingerprint(cases),
            "total": len(cases), "assessed": len(valid), "errors": len(cases) - len(valid),
            "coverage": coverage, "experiments": reports, "riskKindLabeledAssessed": len(labeled), "riskKindsAssessedOnly": kinds,
            "latencyMs": {"p50": percentile(.5), "p95": percentile(.95)},
            "usage": {k: sum(r.get("usage", {}).get(k, 0) for r in results)
                      for k in ("inputTokens", "outputTokens")},
            "results": results,
            "limits": ["Selection experiments are not customer policy or calibrated probabilities.",
                       "Metrics are conditional on assessed rows; coverage and errors must also be shown.",
                       "Candidate classification is not a violation finding or human decision.",
                       "No Backend delivery, hiding, enforcement, production throughput or Phase 1 baseline is proven.",
                       "Token counts are not monetary cost; pricing is not inferred."]}


async def evaluate(cases, *, screen=None, predictions=None):
    results = []
    if (screen is None) == (predictions is None):
        raise ValueError("Choose one live screen or offline prediction map")
    if predictions is not None and set(predictions) != {c.id for c in cases}:
        raise ValueError("Prediction IDs must match selected cases exactly")
    for case in cases:
        started = time.monotonic()
        record = {"id": case.id, "expectedCandidate": case.expected_candidate}
        try:
            if screen is not None:
                assessment, usage = await screen.assess(case.input, trace_id=str(uuid5(NAMESPACE_URL, case.id)))
            else:
                assessment = validate_evidence(case.input, Assessment.model_validate(predictions[case.id]))
                usage = {}
            record.update(status="ASSESSED", riskKinds=sorted(s.kind.value for s in assessment.signals),
                          candidates={name: select_candidate(assessment, name) for name in EXPERIMENTS}, usage=usage)
        except Exception as error:
            # Never serialize exception messages, model text, HTTP bodies or credential values.
            code = error.code if isinstance(error, AgentToolError) else "DETECTOR_ERROR"
            record.update(status="ERROR", errorCode=code if re.fullmatch(r"[A-Z_]{1,64}", str(code)) else "DETECTOR_ERROR")
        record["latencyMs"] = round((time.monotonic() - started) * 1000, 2)
        results.append(record)
    return summarize(cases, results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Opt in to configured model/API usage")
    mode.add_argument("--predictions", type=Path, help="JSON object mapping exact case IDs to assessments")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # Reports cannot overwrite source data or a previous result accidentally.
    if args.out.exists():
        parser.error("Output already exists; choose a new result file")
    if args.limit is not None and not 1 <= args.limit <= 100:
        parser.error("Limit must be in 1..100")
    if args.live and args.limit is None:
        parser.error("Live mode requires an explicit --limit (1..100)")
    try:
        cases = load_cases(args.cases)
        if args.limit is not None:
            cases = cases[:args.limit]
        if args.live:
            report = asyncio.run(evaluate(cases, screen=CandidateScreen(model_from_env(), timeout_seconds=args.timeout)))
            report["execution"] = {"mode": "live-model", "sourceModel": __import__('os').getenv("MODEL_NAME"),
                                   "promptSha256": hashlib.sha256(PROMPT.read_bytes()).hexdigest(),
                                   "timeoutSeconds": args.timeout}
        else:
            predictions = json.loads(args.predictions.read_text())
            if not isinstance(predictions, dict):
                raise ValueError("Predictions must be a JSON object")
            report = asyncio.run(evaluate(cases, predictions=predictions))
            report["execution"] = {"mode": "offline-predictions", "promptExecuted": False}
    except (ValueError, ValidationError, OSError, TypeError, KeyError, json.JSONDecodeError):
        parser.exit(2, "Invalid configuration or input; no report was written.\n")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({"total": report["total"], "assessed": report["assessed"], "errors": report["errors"],
                      "coverage": report["coverage"]}))
    return 1 if report["errors"] else 0
