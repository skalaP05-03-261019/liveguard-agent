"""Investigation evaluation cases and deterministic handoff fixtures. No model, Backend or policy calls.

A case describes one screened target, its context, the candidate hint and the expected
investigation result under each named policy standard. Fixture IDs are derived from the
case ID so the same case always becomes the same InvestigationHandoff.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from app.candidate_detection.contracts import RiskKind, Strength
from app.phase2_bridge.contracts import InvestigationHandoff
from app.schemas import Contract, Judgment

BASE_TIME = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
POLICY_ID = r"^(CP|SP|MP|MM)-\d+\.\d+$"
# Categories are strings so expectations can name a team-approved type (e.g. HATE)
# before the runtime Category enum is extended.
CATEGORY = r"^[A-Z_]{1,32}$"


class Expectation(Contract):
    # Acceptable verdicts; the first one is the primary label used for the confusion matrix.
    verdicts: list[Judgment] = Field(min_length=1, max_length=3)
    categories: list[str] = Field(default_factory=list, max_length=5)
    # Acceptable policy citations. Empty means citation is not scored for this case.
    policy_ids: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def consistent(self):
        if len(set(self.verdicts)) != len(self.verdicts):
            raise ValueError("Duplicate acceptable verdict")
        if len(set(self.categories)) != len(self.categories) or not all(
                re.fullmatch(CATEGORY, c) for c in self.categories):
            raise ValueError("Categories must be unique uppercase identifiers")
        if not all(re.fullmatch(POLICY_ID, p) for p in self.policy_ids):
            raise ValueError("Invalid policy ID")
        if self.verdicts[0] == Judgment.VIOLATION and not self.categories:
            raise ValueError("A primary VIOLATION expectation requires categories")
        if Judgment.VIOLATION not in self.verdicts and self.categories:
            raise ValueError("Categories are only expected for VIOLATION")
        return self


class ContextLine(Contract):
    text: str = Field(min_length=1, max_length=500)
    author: str = Field(default="U2", pattern=r"^[0-9A-Za-z가-힣_]{1,20}$")


class Hint(Contract):
    kind: RiskKind
    strength: Strength


class InvestigationCase(Contract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    split: Literal["development", "confirmation"]
    group: Literal["HATE", "STEREOTYPE", "ATTACK", "VARIANT", "NORMAL", "COUNTER", "SELF", "UNCLEAR",
                   "INJECTION"]
    source_case_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    target: str = Field(min_length=1, max_length=500)
    context: list[ContextLine] = Field(default_factory=list, max_length=8)
    hints: list[Hint] = Field(min_length=1, max_length=4)
    missing_facts: list[str] = Field(default_factory=list, max_length=5)
    expected: dict[str, Expectation] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def valid_case(self):
        if not self.target.strip() or any(not line.text.strip() for line in self.context):
            raise ValueError("Messages must not be blank")
        if len({h.kind for h in self.hints}) != len(self.hints):
            raise ValueError("One hint per risk kind")
        if any(h.strength == Strength.UNCERTAIN for h in self.hints) and not self.missing_facts:
            raise ValueError("Uncertain hints require missing facts")
        if any(line.author == "U1" for line in self.context):
            raise ValueError("U1 is reserved for the target author")
        return self


class CaseSet(Contract):
    version: str = Field(pattern=r"^[a-z0-9.-]{1,40}$")
    review_status: Literal["AUTHOR_DRAFT", "REVIEWED"]
    standards: dict[str, str] = Field(min_length=1)
    cases: list[InvestigationCase] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def complete(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case ID")
        if any(set(c.expected) != set(self.standards) for c in self.cases):
            raise ValueError("Every case needs an expectation for every standard")
        return self


def load_case_set(path) -> CaseSet:
    return CaseSet.model_validate(json.loads(Path(path).read_text()))


def fingerprint(case_set: CaseSet) -> str:
    payload = case_set.model_dump(mode="json", by_alias=True)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _uid(case: InvestigationCase, name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"liveguard:investigation-eval:{case.id}:{name}"))


def build_fixture(case: InvestigationCase, policy_version: str) -> dict:
    """BE-shaped task, snapshot, context and screening result for one case."""
    target_id = _uid(case, "target")
    case_id, snapshot_id, live_id = _uid(case, "case"), _uid(case, "snapshot"), _uid(case, "live")
    lines = [*case.context]
    messages = [{"messageId": _uid(case, f"context-{i}"), "authorAlias": line.author, "text": line.text,
                 "sentAt": (BASE_TIME + timedelta(seconds=i)).isoformat().replace("+00:00", "Z"),
                 "visibility": "PUBLIC", "replyToId": None} for i, line in enumerate(lines)]
    messages.append({"messageId": target_id, "authorAlias": "U1", "text": case.target,
                     "sentAt": (BASE_TIME + timedelta(seconds=len(lines))).isoformat().replace("+00:00", "Z"),
                     "visibility": "HELD", "replyToId": None})
    task = {"eventId": _uid(case, "event"), "taskId": _uid(case, "task"), "caseId": case_id,
            "kind": "ANALYZE", "threadId": f"case:{case_id}:analysis:1", "snapshotId": snapshot_id,
            "expectedEvidenceVersion": 1, "decisionId": None, "traceId": _uid(case, "trace")}
    snapshot = {"caseId": case_id, "snapshotId": snapshot_id, "evidenceVersion": 1,
                "policyVersion": policy_version, "targetMessageIds": [target_id],
                "targetVersions": {target_id: 1}, "liveId": live_id, "userId": _uid(case, "user"),
                "handlingRoute": "CONTEXT_REVIEW", "confirmationStatus": "UNCONFIRMED",
                "targets": [{"messageId": target_id, "visibility": "HELD", "version": 1}]}
    context = {"caseId": case_id, "snapshotId": snapshot_id, "evidenceVersion": 1, "messages": messages}
    screen_result = {
        "requestId": _uid(case, "screen"), "liveId": live_id, "targetMessageId": target_id,
        # include-uncertain selects any reported hint, so every case is an admissible candidate.
        "experiment": "include-uncertain", "selected": True,
        "assessment": {"signals": [{"kind": h.kind.value, "strength": h.strength.value,
                                    "evidence": [{"messageId": target_id, "text": case.target}],
                                    "reason": "평가 사례에 고정된 후보 힌트"} for h in case.hints],
                       "missingFacts": list(case.missing_facts)},
        "usage": {}}
    # Neutral read-tool data. Cases never claim facts the evaluation does not control.
    tools = {"get_user_history": {"userId": snapshot["userId"], "liveId": live_id, "confirmedViolations": []},
             "get_stream_info": {"liveId": live_id, "title": "게임 방송", "authorRole": "UNKNOWN",
                                 "isOfficialOperator": False},
             "get_official_links": {"liveId": live_id, "links": []},
             "get_security_facts": {"caseId": case_id, "facts": []}}
    return {"task": task, "case": snapshot, "context": context, "screenResult": screen_result, "tools": tools}


def build_handoff(case: InvestigationCase, policy_version: str) -> InvestigationHandoff:
    fixture = build_fixture(case, policy_version)
    return InvestigationHandoff.model_validate({"screening": fixture["screenResult"], "task": fixture["task"],
                                                "snapshot": fixture["case"],
                                                "messages": fixture["context"]["messages"]})


class FixtureBusiness:
    """Read-only Business MCP stand-in for one case. Records tool names only."""

    def __init__(self, fixture: dict):
        self.fixture, self.calls = fixture, []

    async def call(self, name, arguments):
        self.calls.append(name)
        if name == "get_moderation_case":
            data = self.fixture["case"]
        elif name == "get_chat_context":
            data = self.fixture["context"]
        else:
            data = self.fixture["tools"][name]
        return {"data": deepcopy(data), "traceId": arguments["traceId"]}
