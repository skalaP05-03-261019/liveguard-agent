"""Agent-owned experimental contracts; not an agreed Backend API or new violation enum."""

from enum import StrEnum
from uuid import UUID

from pydantic import Field, model_validator
from app.schemas import Contract


class RiskKind(StrEnum):
    REGION_DISPARAGEMENT = "REGION_DISPARAGEMENT"
    GROUP_HATE = "GROUP_HATE"
    TARGETED_ATTACK = "TARGETED_ATTACK"
    PROFANITY_VARIANT = "PROFANITY_VARIANT"


class Strength(StrEnum):
    CLEAR = "CLEAR"
    POSSIBLE = "POSSIBLE"
    UNCERTAIN = "UNCERTAIN"


class ChatMessage(Contract):
    message_id: UUID
    text: str = Field(min_length=1, max_length=500, strict=True)

    @model_validator(mode="after")
    def nonblank(self):
        if not self.text.strip():
            raise ValueError("Message text must not be blank")
        return self


class ScreeningInput(Contract):
    live_id: UUID
    target: ChatMessage
    context: list[ChatMessage] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_messages(self):
        ids = [self.target.message_id, *(m.message_id for m in self.context)]
        if len(ids) != len(set(ids)):
            raise ValueError("Target/context message IDs must be distinct")
        return self


class Quote(Contract):
    message_id: UUID
    text: str = Field(min_length=1, max_length=500, strict=True)


class RiskSignal(Contract):
    kind: RiskKind
    strength: Strength
    evidence: list[Quote] = Field(min_length=1, max_length=4)
    reason: str = Field(min_length=1, max_length=400, strict=True)

    @model_validator(mode="after")
    def meaningful_reason(self):
        if not self.reason.strip():
            raise ValueError("Reason must not be blank")
        return self


class Assessment(Contract):
    signals: list[RiskSignal] = Field(default_factory=list, max_length=4)
    missing_facts: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def uncertainty_and_duplicates(self):
        kinds = [s.kind for s in self.signals]
        if len(kinds) != len(set(kinds)):
            raise ValueError("One signal per risk kind")
        if any(not isinstance(f, str) or not f.strip() or len(f) > 300 for f in self.missing_facts):
            raise ValueError("Missing facts must be short nonblank strings")
        if any(s.strength == Strength.UNCERTAIN for s in self.signals) and not self.missing_facts:
            raise ValueError("Uncertain signals require missing facts")
        if self.missing_facts and not self.signals:
            raise ValueError("Missing context must be tied to a signal, not silently treated as no signal")
        return self


# Selection experiments, not customer defaults, probabilities, hiding or sanction rules.
EXPERIMENTS = {
    "clear-only": frozenset({Strength.CLEAR}),
    "clear-or-possible": frozenset({Strength.CLEAR, Strength.POSSIBLE}),
    "include-uncertain": frozenset(Strength),
}


def select_candidate(assessment: Assessment, experiment: str) -> bool:
    if experiment not in EXPERIMENTS:
        raise ValueError("Unknown selection experiment")
    return any(s.strength in EXPERIMENTS[experiment] for s in assessment.signals)


def validate_evidence(data: ScreeningInput, assessment: Assessment) -> Assessment:
    messages = {m.message_id: m.text for m in [data.target, *data.context]}
    for signal in assessment.signals:
        if not any(e.message_id == data.target.message_id for e in signal.evidence):
            raise ValueError("Context alone cannot nominate a different target")
        for quote in signal.evidence:
            if quote.message_id not in messages or not quote.text.strip() or quote.text not in messages[quote.message_id]:
                raise ValueError("Evidence must quote a supplied message exactly")
    return assessment
