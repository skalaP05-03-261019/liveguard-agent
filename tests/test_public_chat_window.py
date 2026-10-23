"""The Agent must not pass private or mixed-live chat to a public summarizer."""

from copy import deepcopy
import unittest

from pydantic import ValidationError

from app.integration.public_chat_window import PublicChatWindow


LIVE = "00000000-0000-4000-8000-000000000001"
OTHER_LIVE = "00000000-0000-4000-8000-000000000002"
MESSAGE = "00000000-0000-4000-8000-000000000003"


def window():
    return {"liveId": LIVE, "windowStart": "2026-10-23T06:00:00Z",
            "windowEnd": "2026-10-23T06:05:00Z", "complete": True,
            "messages": [{"messageId": MESSAGE, "liveId": LIVE,
                          "text": "오늘 방송 재미있어요", "sentAt": "2026-10-23T06:01:00Z",
                          "visibility": "PUBLIC", "version": 1}]}


class PublicChatWindowTests(unittest.TestCase):
    def test_valid_public_window_and_stable_ordering(self):
        data = window()
        later = {**data["messages"][0],
                 "messageId": "00000000-0000-4000-8000-000000000004",
                 "sentAt": "2026-10-23T06:02:00Z"}
        data["messages"] = [later, data["messages"][0]]
        parsed = PublicChatWindow.model_validate(data)
        self.assertEqual(str(parsed.ordered_messages()[0].message_id), MESSAGE)
        self.assertEqual(parsed.source_fingerprint(),
                         PublicChatWindow.model_validate({**data,
                             "messages": list(reversed(data["messages"]))}).source_fingerprint())

    def test_empty_public_window_is_valid_input_for_insufficient_chat(self):
        data = window()
        data["messages"] = []
        self.assertEqual(PublicChatWindow.model_validate(data).ordered_messages(), ())

    def test_private_incomplete_or_cross_live_window_is_rejected(self):
        for mutate in (
            lambda data: data["messages"][0].update(visibility="HELD"),
            lambda data: data["messages"][0].update(visibility="HIDDEN"),
            lambda data: data.update(complete=False),
            lambda data: data["messages"][0].update(liveId=OTHER_LIVE),
            lambda data: data["messages"].append(deepcopy(data["messages"][0])),
            lambda data: data["messages"][0].update(sentAt=data["windowEnd"]),
            lambda data: data.update(windowEnd=data["windowStart"]),
            lambda data: data.update(windowStart="2026-10-23T06:00:00"),
            lambda data: data["messages"][0].update(version=0),
        ):
            data = window()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(ValidationError):
                PublicChatWindow.model_validate(data)

    def test_fingerprint_changes_for_correction_or_visibility_version(self):
        original = window()
        digest = PublicChatWindow.model_validate(original).source_fingerprint()
        changed_text = deepcopy(original)
        changed_text["messages"][0]["text"] = "정정된 채팅"
        changed_version = deepcopy(original)
        changed_version["messages"][0]["version"] = 2
        self.assertNotEqual(digest, PublicChatWindow.model_validate(changed_text).source_fingerprint())
        self.assertNotEqual(digest, PublicChatWindow.model_validate(changed_version).source_fingerprint())
