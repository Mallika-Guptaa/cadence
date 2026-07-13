import unittest
from unittest.mock import patch

from cadence.agenda import draft_agenda, draft_handover
from cadence.context_search import ContextItem

ITEMS = [
    ContextItem(title="#general", content="Q3 launch planning kicked off, docs draft due Friday", permalink="https://x/1"),
    ContextItem(title="#eng", content="Payments migration blocked on schema review", permalink="https://x/2"),
]


class AgendaTests(unittest.TestCase):
    @patch("cadence.agenda.draft", return_value=None)
    def test_deterministic_agenda_uses_context(self, _):
        text = draft_agenda("q3 launch", ["You", "Priya"], ITEMS)
        self.assertIn("Q3 launch planning", text)
        self.assertIn("Related:", text)
        self.assertIn("https://x/1", text)

    @patch("cadence.agenda.draft", return_value=None)
    def test_agenda_without_context_is_generic_but_valid(self, _):
        text = draft_agenda("roadmap", ["You"], [])
        self.assertIn("roadmap", text)
        self.assertNotIn("Related:", text)

    @patch("cadence.agenda.draft", return_value="- point one\n- point two")
    def test_llm_agenda_gets_links_appended(self, _):
        text = draft_agenda("q3 launch", ["You"], ITEMS)
        self.assertTrue(text.startswith("- point one"))
        self.assertIn("Related:", text)


class HandoverTests(unittest.TestCase):
    @patch("cadence.agenda.draft", return_value=None)
    def test_deterministic_handover(self, _):
        event = {"title": "Q3 launch review", "start": "2026-07-16T10:00:00-07:00"}
        text = draft_handover(event, "Marco", ITEMS)
        self.assertIn("Marco", text)
        self.assertIn("Q3 launch review", text)
        self.assertIn("Related:", text)

    @patch("cadence.agenda.draft", return_value=None)
    def test_handover_no_context_flags_it(self, _):
        event = {"title": "Standup", "start": "2026-07-17T09:30:00-07:00"}
        text = draft_handover(event, "Priya", [])
        self.assertIn("No recent workspace discussion", text)


if __name__ == "__main__":
    unittest.main()
