import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cadence.features import FeatureContext, catchup
from cadence.store import Store

TZ = ZoneInfo("America/Los_Angeles")
# Saturday 2026-07-11 — matches the real demo anchor
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=TZ)


def blob(blocks: list[dict]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


class CatchupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.db")
        # no LLM by default (deterministic path); no permalinks (client=None)
        draft_patcher = patch("cadence.llm.draft", return_value=None)
        draft_patcher.start()
        self.addCleanup(draft_patcher.stop)

    def ctx(self, **overrides) -> FeatureContext:
        defaults = dict(
            store=self.store, mcp=lambda: None, client=None,
            user_id="U123", user_name="Priya", now=NOW,
        )
        defaults.update(overrides)
        return FeatureContext(**defaults)

    def seed(self, channel: str, days_ago: float, text: str, user_name: str = "Marco"):
        ts = f"{(NOW - timedelta(days=days_ago)).timestamp():.6f}"
        self.store.upsert_message(f"C_{channel}", f"#{channel}", ts, "U999", user_name, text)

    # -- window parsing --------------------------------------------------------

    def test_parse_window_since_weekday(self):
        self.assertEqual(catchup._parse_window("away since monday", NOW).date().isoformat(), "2026-07-06")
        self.assertEqual(catchup._parse_window("away since saturday", NOW).date().isoformat(), "2026-07-04")

    def test_parse_window_days_yesterday_default(self):
        self.assertEqual(catchup._parse_window("i was out 3 days", NOW), NOW - timedelta(days=3))
        yesterday = catchup._parse_window("i was out yesterday", NOW)
        self.assertEqual(yesterday.date().isoformat(), "2026-07-10")
        self.assertEqual((yesterday.hour, yesterday.minute), (0, 0))
        self.assertEqual(catchup._parse_window("catch me up", NOW), NOW - timedelta(days=2))

    def test_window_filters_messages(self):
        self.seed("eng", 3, "the deploy went out to production cleanly")   # inside "since monday"
        self.seed("eng", 6, "old news from last sunday, ignore this one")   # outside
        blocks, fallback = catchup.handle("what did i miss since monday", self.ctx())
        text = blob(blocks)
        self.assertIn("Since Mon Jul 6", text)
        self.assertIn("the deploy went out", text)
        self.assertNotIn("old news", text)
        self.assertIn("1 messages", fallback)

    # -- mentions vs channel highlights ---------------------------------------

    def test_mention_section_and_channel_highlights(self):
        self.seed("eng", 0.5, "<@U123> can you review the migration before we ship?")
        self.seed("general", 0.6, "standup moved to 10am tomorrow, please note")
        self.seed("general", 0.7, "we decided to approve the new lunch vendor")
        blocks, fallback = catchup.handle("catch me up", self.ctx())
        text = blob(blocks)
        self.assertIn("You were mentioned (1)", text)
        self.assertIn("can you review the migration", text)
        self.assertIn("#general", text)          # channel highlight section present
        self.assertIn("we decided to approve", text)  # signal-ranked highlight surfaced
        self.assertIn("1 mention you", fallback)

    def test_no_mentions_shows_honest_line(self):
        self.seed("general", 0.6, "just some ordinary chatter here in the channel")
        blocks, _ = catchup.handle("catch me up", self.ctx())
        self.assertIn("You were mentioned (0)", blob(blocks))
        self.assertIn("Nobody tagged you", blob(blocks))

    def test_name_match_case_insensitive_but_not_placeholder(self):
        self.seed("eng", 0.5, "someone should ask PRIYA about the rollback plan")
        self.assertIn("You were mentioned (1)", blob(catchup.handle("catch me up", self.ctx())[0]))
        # 'you' placeholder must not match every message
        self.assertIn("You were mentioned (0)", blob(catchup.handle("catch me up", self.ctx(user_name="you"))[0]))

    def test_highlights_dedupe_and_cap(self):
        # 6 identical + 2 distinct in one channel -> at most 3 bullets, no dupes
        for i in range(6):
            self.seed("eng", 0.5 + i * 0.001, "I can pair on the redis work tomorrow if that helps")
        self.seed("eng", 0.4, "we shipped the checkout fix to production")
        self.seed("eng", 0.3, "the gateway outage is fully resolved now")
        blocks, _ = catchup.handle("catch me up", self.ctx())
        text = blob(blocks)
        self.assertEqual(text.count("I can pair on the redis"), 1)  # deduped
        self.assertIn("we shipped the checkout fix", text)          # signal wins
        self.assertIn("gateway outage is fully resolved", text)

    # -- card shape / budget ---------------------------------------------------

    def test_empty_store(self):
        blocks, fallback = catchup.handle("catch me up", self.ctx())
        self.assertIn("Nothing new since Thu Jul 9", fallback)
        self.assertIn("Nothing new since Thu Jul 9", blob(blocks))

    def test_channel_budget_and_details(self):
        for i in range(6):
            self.seed(f"chan{i}", 0.5 + i * 0.01, f"a distinct update number {i} worth showing")
        concise = blob(catchup.handle("catch me up", self.ctx())[0])
        self.assertIn("more channel(s)", concise)         # 6 channels, 3 shown
        detailed = blob(catchup.handle("catch me up with details", self.ctx())[0])
        self.assertNotIn("more channel(s)", detailed)     # all 6 fit under detail cap

    def test_llm_tldr_included_when_available(self):
        self.seed("eng", 0.5, "the build is red and needs attention")
        with patch("cadence.llm.draft", return_value="Build is red, needs a fix"):
            blocks, _ = catchup.handle("catch me up", self.ctx())
        self.assertIn("TL;DR", blob(blocks))
        self.assertIn("Build is red", blob(blocks))

    def test_never_raises(self):
        blocks, fallback = catchup.handle("catch me up", self.ctx(store=None))
        self.assertIn(":warning:", blob(blocks))
        self.assertTrue(fallback)


if __name__ == "__main__":
    unittest.main()
