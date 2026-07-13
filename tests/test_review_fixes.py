"""Regression tests for the adversarial-review fixes."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cadence import slack_app, style
from cadence.context_search import RetrievalTrace
from cadence.features.promises import _parse_due
from cadence.intent import Intent, resolve_leave_dates

TZ = ZoneInfo("America/Los_Angeles")


class RoutingPrecedenceTests(unittest.TestCase):
    def test_meeting_ask_mentioning_release_notes_prefers_scheduling(self):
        self.assertTrue(slack_app._SCHEDULING_PRIORITY_RE.search("find time to discuss the release notes"))

    def test_sso_setup_does_not_trigger_scheduling_priority(self):
        text = "was SSO setup discussed before?"
        self.assertFalse(slack_app._SCHEDULING_PRIORITY_RE.search(text))
        self.assertFalse(slack_app._LEAVE_PRIORITY_RE.search(text))

    def test_leave_phrase_priority(self):
        self.assertTrue(slack_app._LEAVE_PRIORITY_RE.search("I'm on leave Thursday — find cover for my meetings"))


class MarkupSanitationTests(unittest.TestCase):
    def test_mentions_channels_links_flattened(self):
        raw = "ping <@U123ABC> about <#C42|general> — see <https://x.y/z|the doc> and <https://plain.url>"
        cleaned = style.clean_slack_markup(raw)
        self.assertNotIn("<@", cleaned)
        self.assertIn("#general", cleaned)
        self.assertIn("the doc", cleaned)
        self.assertNotIn("<https://x.y/z|", cleaned)

    def test_at_here_neutralized(self):
        self.assertEqual(style.clean_slack_markup("<!here> deploy done"), "@here deploy done")

    def test_link_label_cannot_break_token(self):
        label = "a|b>c"
        self.assertNotIn("|a|", style.link("https://x", label))
        self.assertNotIn(">c", style.link("https://x", label).split("|")[1][:-1])


class FooterHonestyTests(unittest.TestCase):
    def test_history_only_footer_does_not_claim_rts(self):
        footer = RetrievalTrace(history=7).footer()
        self.assertNotIn("assistant.search.context", footer)
        self.assertNotIn("Real-Time Search", footer)

    def test_rts_footer_names_the_api(self):
        footer = RetrievalTrace(rts_user=3, history=4).footer()
        self.assertIn("Real-Time Search API (3 results)", footer)
        self.assertIn("assistant.search.context", footer)


class PromiseDueTests(unittest.TestCase):
    def test_eod_after_5pm_rolls_to_next_day(self):
        ref = datetime(2026, 7, 13, 18, 30, tzinfo=TZ).timestamp()  # Monday 6:30pm
        due = _parse_due("I'll send it by EOD", ref)
        self.assertEqual(datetime.fromtimestamp(due, TZ).day, 14)

    def test_eod_before_5pm_is_same_day(self):
        ref = datetime(2026, 7, 13, 10, 0, tzinfo=TZ).timestamp()
        due = _parse_due("I'll send it by EOD", ref)
        self.assertEqual(datetime.fromtimestamp(due, TZ).day, 13)


class IdempotencyTests(unittest.TestCase):
    def test_second_identical_click_short_circuits(self):
        slack_app._handled_actions.clear()
        self.assertFalse(slack_app._already_handled("book_slot_0", '{"start":"x"}'))
        self.assertTrue(slack_app._already_handled("book_slot_0", '{"start":"x"}'))
        self.assertFalse(slack_app._already_handled("book_slot_1", '{"start":"x"}'))


class LeaveDatesTests(unittest.TestCase):
    def test_non_contiguous_days_only_matched_dates(self):
        now = datetime(2026, 7, 11, 12, 0, tzinfo=TZ)  # Saturday
        intent = Intent(kind="leave_coverage", leave_days=["monday", "friday"])
        dates = resolve_leave_dates(intent, now)
        self.assertEqual([d.isoformat() for d in dates], ["2026-07-13", "2026-07-17"])


if __name__ == "__main__":
    unittest.main()
