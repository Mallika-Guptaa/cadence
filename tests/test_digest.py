import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from cadence.digest import build_digest_blocks, seconds_until_next
from cadence.features import FeatureContext
from cadence.store import Store

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 7, 13, 8, 30, tzinfo=TZ)  # Monday morning


def make_ctx(events=None, with_promises=True, with_messages=True):
    store = Store(tempfile.mktemp(suffix=".db"))
    now_ts = NOW.timestamp()
    if with_promises:
        store.add_promise("U1", "Priya", "send the deck", now_ts - 3600, "C1", "1.1", None)  # overdue
        store.add_promise("U2", "Marco", "fix the bug", now_ts + 86400, "C1", "1.2", None)
    if with_messages:
        store.upsert_message("C1", "#general", f"{now_ts - 7200:.4f}", "U1", "Priya", "hello world message")
        store.upsert_message("C2", "#infra", f"{now_ts - 3600:.4f}", "U2", "Marco", "another message here")
        store.upsert_message("C2", "#infra", f"{now_ts - 1800:.4f}", "U2", "Marco", "third message text")
    mcp = MagicMock()
    mcp.call_tool.return_value = events if events is not None else []
    return FeatureContext(store=store, mcp=lambda: mcp, client=None, now=NOW)


class SecondsUntilNextTests(unittest.TestCase):
    def test_before_hour_today(self):
        self.assertAlmostEqual(seconds_until_next(9, NOW), 1800, delta=1)  # 8:30 -> 9:00

    def test_after_hour_rolls_to_tomorrow(self):
        late = NOW.replace(hour=10)  # 10:30 -> next 9:00 is 22.5h away
        self.assertAlmostEqual(seconds_until_next(9, late), 22.5 * 3600, delta=1)


class DigestBlocksTests(unittest.TestCase):
    def test_full_digest(self):
        events = [
            {"kind": "meeting", "title": "Q3 review", "start": "2026-07-13T10:00:00-07:00", "end": "2026-07-13T10:45:00-07:00"},
            {"kind": "leave", "title": "On leave", "start": "2026-07-13T00:00:00-07:00", "end": "2026-07-13T23:59:00-07:00"},
        ]
        blocks, text = build_digest_blocks(make_ctx(events=events), "alex@cadence.demo")
        joined = str(blocks)
        self.assertIn("Daily Cadence", joined)
        self.assertIn("10:00 — Q3 review", joined)
        self.assertNotIn("On leave", joined)  # non-meetings excluded
        self.assertIn("Open promises (2, 1 overdue)", joined)
        self.assertIn(":red_circle:", joined)   # overdue marked
        self.assertIn("#infra (2)", joined)     # busiest channel
        self.assertEqual(text, "Daily Cadence digest")

    def test_empty_calendar_and_mcp_down(self):
        ctx = make_ctx()
        ctx.mcp = lambda: (_ for _ in ()).throw(RuntimeError("down"))  # mcp() raises
        blocks, _ = build_digest_blocks(ctx, "alex@cadence.demo")
        self.assertIn("No meetings", str(blocks))  # digest still builds

    def test_quiet_workspace(self):
        blocks, _ = build_digest_blocks(make_ctx(with_promises=False, with_messages=False), "a@d")
        joined = str(blocks)
        self.assertIn("No meetings", joined)
        self.assertNotIn("Open promises", joined)
        self.assertNotIn("Last 24h", joined)


if __name__ == "__main__":
    unittest.main()
