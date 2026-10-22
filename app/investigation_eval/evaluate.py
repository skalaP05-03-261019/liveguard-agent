"""Investigation-quality metrics on fixed cases. Reports omit chat text, reasons and policy text.

Each case runs through the same read-only handoff path as #63 (fixture BE reads, real or
injected investigator, existing output validation). Scoring compares the target finding
with the case expectation for one named policy standard.
"""

from collections import Counter
import hashlib
import math
from pathlib import Path
import re
import subprocess
import time

from pydantic import Field

from app.clients.business_mcp import AgentToolError
from app.phase2_bridge.handoff import run_readonly_investigation
from app.schemas import Contract, Judgment
from .cases import CaseSet, FixtureBusiness, build_fixture, build_handoff, fingerprint

OUTCOMES = ("MATCH", "MISSED_VIOLATION", "HELD_VIOLATION", "FALSE_VIOLATION", "UNSUPPORTED_NORMAL", "OVER_HOLD")
# Files that define the investigator under test. A baseline is valid only if these match phase1-end.
INVESTIGATOR_SOURCES = ("prompts/investigator.md", "app/investigation/runner.py", "app/investigation/model.py",
                        "app/investigation/validation.py", "app/investigation/graph.py", "app/nodes.py",
                        "app/policy_evidence.py", "app/harness/execution.py", "app/harness/safety.py",
                        "app/phase2_bridge/handoff.py")
ROOT = Path(__file__).resolve().parents[2]


class Prediction(Contract):
    """Target finding as scored. Categories are strings so future types can be replayed."""
    verdict: Judgment
    categories: list[str] = Field(default_factory=list)
    policy_ids: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)


def outcome(expectation, verdict) -> str:
    if verdict in expectation.verdicts:
        return "MATCH"
    if verdict == Judgment.VIOLATION:
        return "FALSE_VIOLATION"
    if Judgment.VIOLATION in expectation.verdicts:
        return "HELD_VIOLATION" if verdict == Judgment.NEEDS_REVIEW else "MISSED_VIOLATION"
    # Expected a hold only; a definite normal verdict is an unsupported assertion.
    return "UNSUPPORTED_NORMAL" if verdict == Judgment.NO_VIOLATION else "OVER_HOLD"


def score(case, standard, prediction: Prediction) -> dict:
    expected = case.expected[standard]
    verdict_match = prediction.verdict in expected.verdicts
    category_match = None
    if prediction.verdict == Judgment.VIOLATION and Judgment.VIOLATION in expected.verdicts:
        category_match = set(prediction.categories) == set(expected.categories)
    policy_cited = bool(set(prediction.policy_ids) & set(expected.policy_ids)) if expected.policy_ids else None
    # A definite verdict must cite an expected clause; a hold is not penalized for citations.
    satisfied = (verdict_match and category_match is not False
                 and (policy_cited is not False or prediction.verdict == Judgment.NEEDS_REVIEW))
    return {"expectedVerdict": expected.verdicts[0].value, "verdict": prediction.verdict.value,
            "outcome": outcome(expected, prediction.verdict), "verdictMatch": verdict_match,
            "categoryMatch": category_match, "policyCited": policy_cited, "satisfied": satisfied,
            "categories": sorted(prediction.categories), "policyIds": sorted(prediction.policy_ids),
            "missingFactCount": len(prediction.missing_facts)}


def ratio(n, d):
    return round(n / d, 4) if d else None


def _group_stats(rows):
    return {"total": len(rows), "assessed": sum(r["status"] == "ASSESSED" for r in rows),
            "satisfied": sum(r.get("satisfied", False) for r in rows),
            "verdictMatch": sum(r.get("verdictMatch", False) for r in rows)}


def summarize(case_set: CaseSet, cases, standard, results) -> dict:
    if [c.id for c in cases] != [r["id"] for r in results]:
        raise ValueError("Results must match cases exactly")
    assessed = [r for r in results if r["status"] == "ASSESSED"]
    outcomes = Counter(r["outcome"] for r in assessed)
    labels = [j.value for j in Judgment]
    confusion = {e: {a: sum(r["expectedVerdict"] == e and r["verdict"] == a for r in assessed) for a in labels}
                 for e in labels}
    cited = [r for r in assessed if r["policyCited"] is not None and r["verdict"] != Judgment.NEEDS_REVIEW]
    latencies = sorted(r["latencyMs"] for r in results)
    percentile = lambda p: latencies[max(0, math.ceil(len(latencies) * p) - 1)] if latencies else None
    by = lambda key: {value: _group_stats([r for r in results if r[key] == value])
                      for value in sorted({r[key] for r in results})}
    return {"kind": "investigation-quality", "caseSetVersion": case_set.version,
            "reviewStatus": case_set.review_status, "standard": standard,
            "standardDescription": case_set.standards[standard], "datasetSha256": fingerprint(case_set),
            "total": len(results), "assessed": len(assessed), "errors": len(results) - len(assessed),
            "coverage": ratio(len(assessed), len(results)),
            "satisfied": sum(r["satisfied"] for r in assessed),
            "satisfiedRateOfTotal": ratio(sum(r["satisfied"] for r in assessed), len(results)),
            "verdictMatch": sum(r["verdictMatch"] for r in assessed),
            "outcomes": {name: outcomes.get(name, 0) for name in OUTCOMES},
            "unsupportedAssertions": outcomes.get("FALSE_VIOLATION", 0) + outcomes.get("UNSUPPORTED_NORMAL", 0),
            "confusionExpectedByActual": confusion,
            "policyCitation": {"applicable": len(cited), "cited": sum(r["policyCited"] for r in cited)},
            "bySplit": by("split"), "byGroup": by("group"),
            "latencyMs": {"p50": percentile(.5), "p95": percentile(.95)},
            "usage": {k: sum(r.get("usage", {}).get(k, 0) for r in results) for k in ("inputTokens", "outputTokens")},
            "modelCalls": sum(r.get("modelCalls", 0) for r in results),
            "results": results,
            "limits": ["Expected labels are author drafts until reviewStatus is REVIEWED.",
                       "Fixture Backend reads; no task acceptance, hiding, sanction or human decision is executed.",
                       "Satisfied requires verdict, category and (for definite verdicts) an expected policy citation.",
                       "Development and confirmation splits must not be merged into one accuracy claim.",
                       "Token counts are not monetary cost; latency is sequential per-case, not throughput."]}


class UsageMeter:
    """Pass-through model wrapper that adds per-call token usage."""

    def __init__(self, model):
        self.model, self.usage = model, Counter()

    async def complete(self, messages, tools, *, trace_id):
        turn = await self.model.complete(messages, tools, trace_id=trace_id)
        for key in ("inputTokens", "outputTokens"):
            value = turn.usage.get(key) if isinstance(turn.usage, dict) else None
            if type(value) is int and value >= 0:
                self.usage[key] += value
        return turn


async def evaluate(case_set: CaseSet, cases, standard, *, policy_version, investigator_factory=None,
                   predictions=None, meter: UsageMeter | None = None, timeout_seconds=120):
    """Return (report, details). Details hold model reasons/missing facts and stay local."""
    if standard not in case_set.standards:
        raise ValueError("Unknown standard")
    if (investigator_factory is None) == (predictions is None):
        raise ValueError("Choose one investigator or offline prediction map")
    if predictions is not None and set(predictions) != {c.id for c in cases}:
        raise ValueError("Prediction IDs must match selected cases exactly")
    results, details = [], []
    for case in cases:
        started = time.monotonic()
        before = Counter(meter.usage) if meter else Counter()
        record = {"id": case.id, "split": case.split, "group": case.group, "sourceCaseId": case.source_case_id}
        try:
            if predictions is not None:
                prediction = Prediction.model_validate(predictions[case.id])
                detail = {}
            else:
                fixture = build_fixture(case, policy_version)
                business = FixtureBusiness(fixture)
                draft = await run_readonly_investigation(build_handoff(case, policy_version),
                                                         investigator_factory(business),
                                                         timeout_seconds=timeout_seconds)
                finding = draft.analysis.message_findings[0]
                prediction = Prediction(verdict=finding.verdict, categories=[c.value for c in finding.categories],
                                        policy_ids=finding.policy_ids, missing_facts=finding.missing_facts)
                record.update(modelCalls=int(draft.harness_budget.get("modelCalls", 0)),
                              readTools=sorted(set(business.calls)))
                detail = {"reason": finding.reason}
            record.update(status="ASSESSED", **score(case, standard, prediction))
            # Same shape as a Prediction, so a details file can be replayed under another standard.
            details.append({"id": case.id, "verdict": prediction.verdict.value, "categories": prediction.categories,
                            "policyIds": prediction.policy_ids, "missingFacts": prediction.missing_facts, **detail})
        except Exception as error:
            # Never serialize exception messages, model text, HTTP bodies or credential values.
            code = error.code if isinstance(error, AgentToolError) else "INVESTIGATOR_ERROR"
            record.update(status="ERROR", errorCode=code if re.fullmatch(r"[A-Z_]{1,64}", str(code))
                          else "INVESTIGATOR_ERROR")
        if meter:
            record["usage"] = {k: meter.usage[k] - before[k] for k in ("inputTokens", "outputTokens")}
        record["latencyMs"] = round((time.monotonic() - started) * 1000, 2)
        results.append(record)
    return summarize(case_set, cases, standard, results), details


def source_fingerprint(root: Path = ROOT) -> dict:
    files = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in INVESTIGATOR_SOURCES}
    combined = hashlib.sha256("".join(f"{p}:{h}\n" for p, h in sorted(files.items())).encode()).hexdigest()
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {"gitCommit": commit, "investigatorSourcesSha256": combined, "files": files}
