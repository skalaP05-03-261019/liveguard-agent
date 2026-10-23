"""The Phase 2 investigator must not combine contradictory policy instructions."""

import unittest

from app.phase2_investigation.investigator import system_prompt


class Phase2PolicyPromptTests(unittest.TestCase):
    def test_group_hate_boundary_is_version_specific(self):
        old = system_prompt("v3.1")
        new = system_prompt("v3.2")
        self.assertIn("CP-6.1", old)
        self.assertIn("집단 혐오를 HARASSMENT로 바꿔 판정하지 마세요", old)
        self.assertNotIn("CP-3.3", old)
        self.assertIn("CP-3.3", new)
        self.assertIn("기존 HARASSMENT", new)
        self.assertNotIn("집단 혐오를 HARASSMENT로 바꿔 판정하지 마세요", new)

    def test_legacy_fixture_uses_old_addendum(self):
        self.assertEqual(system_prompt("fixture-policy-v1"),
                         system_prompt("v3.1"))


if __name__ == "__main__":
    unittest.main()
