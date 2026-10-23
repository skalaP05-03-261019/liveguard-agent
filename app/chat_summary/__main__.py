"""Run authored summary cases against the configured model for human fact/evidence review.

Live only (explicit --live); writes a new local file and never overwrites. Cases are authored
chat, not real users' chat. Usage: python -m app.chat_summary --cases FILE --live --out FILE
"""

import argparse
import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path

from app.investigation.model import model_from_env
from .schemas import SummaryInput
from .summarize import summarize


def build_inputs(raw):
    start = datetime.fromisoformat(raw["windowStart"])
    for case in raw["cases"]:
        messages = [{"messageId": m["messageId"], "liveId": raw["liveId"], "text": m["text"],
                     "sentAt": (start + timedelta(minutes=m["minute"])).isoformat(),
                     "visibility": "PUBLIC", "version": 1} for m in case["messages"]]
        yield case, SummaryInput.model_validate({"liveId": raw["liveId"], "windowStart": raw["windowStart"],
                                                 "windowEnd": raw["windowEnd"], "messages": messages})


async def run(raw, model, model_version):
    rows = []
    for case, request in build_inputs(raw):
        result = await summarize(request, model=model, trace_id=f"chat-summary-{case['id']}",
                                 model_version=model_version)
        rows.append({"id": case["id"], "group": case["group"], "messageCount": len(request.messages),
                     **result.model_dump(mode="json", by_alias=True)})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--live", action="store_true", required=True, help="Opt in to configured model usage")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.cases.read_text(encoding="utf-8"))
    rows = asyncio.run(run(raw, model_from_env(), os.getenv("MODEL_NAME")))
    report = {"caseSetVersion": raw["version"], "reviewStatus": raw["reviewStatus"], "results": rows}
    with args.out.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    for row in rows:
        print(row["id"], row["status"], row.get("errorCode"),
              len(row["topics"]), len(row["questionsOrSuggestions"]), len(row["unresolvedItems"]))


if __name__ == "__main__":
    main()
