"""UUID evidence references must survive masking while chat PII stays hidden."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import unittest
from uuid import UUID

from app.chat_summary.schemas import SummaryInput, SummaryStatus
from app.chat_summary.summarize import summarize
from app.investigation.model import ModelTurn, ToolCall


LIVE = UUID("11111111-1111-4111-8111-111111111111")
# This valid UUID contains a phone/account-like substring, caught by generic text masking.
EVIDENCE = UUID("4539e436-7173-4215-a3dd-33e58a795e7a")
START = datetime(2026, 10, 23, 6, tzinfo=timezone.utc)


class Model:
    def __init__(self):
        self.inputs = []

    async def complete(self, messages, tools, *, trace_id):
        self.inputs.append(messages)
        return ModelTurn([ToolCall("1", "submit_summary", {
            "topics": [{"text": "다음 보스 공략 이야기", "evidenceMessageIds": [str(EVIDENCE)]}],
            "questionsOrSuggestions": [], "unresolvedItems": [],
        })], {"inputTokens": 10, "outputTokens": 5})


class SummaryIdentifierMaskingTests(unittest.TestCase):
    def test_model_receives_original_evidence_id_but_masked_phone_text(self):
        ids = [EVIDENCE, UUID("11111111-2222-4333-8444-555555555555"),
               UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")]
        input_ = SummaryInput.model_validate({
            "liveId": str(LIVE), "windowStart": START.isoformat(),
            "windowEnd": (START + timedelta(minutes=5)).isoformat(),
            "messages": [{"messageId": str(mid), "liveId": str(LIVE),
                          "text": text, "sentAt": (START + timedelta(minutes=i)).isoformat(),
                          "visibility": "PUBLIC", "version": 1}
                         for i, (mid, text) in enumerate(zip(ids, (
                             "연락처 010-1234-5678", "다음 보스는 언제?", "공략 알려주세요")))],
        })
        model = Model()
        result = asyncio.run(summarize(input_, model=model, trace_id="summary-id-mask"))
        self.assertEqual(result.status, SummaryStatus.SUCCESS)
        sent = json.loads(model.inputs[0][1]["content"])
        self.assertEqual(sent["messages"][0]["messageId"], str(EVIDENCE))
        self.assertEqual(sent["messages"][0]["text"], "연락처 [PHONE]")
        self.assertEqual(result.evidence_ids(), {EVIDENCE})


if __name__ == "__main__":
    unittest.main()
