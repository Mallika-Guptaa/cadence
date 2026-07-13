import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cadence.intent import Intent, extract_intent, regex_intent, resolve_leave_window, resolve_meeting_window

TZ = ZoneInfo("America/Los_Angeles")
NAMES = ["alex", "priya", "marco", "dana"]
# Saturday 2026-07-11 — matches the real demo anchor
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=TZ)


class RegexIntentTests(unittest.TestCase):
    def test_meeting_request(self):
        intent = regex_intent("Find 45 minutes for me, Priya and Marco this week", NAMES)
        self.assertEqual(intent.kind, "schedule_meeting")
        self.assertEqual(intent.duration_minutes, 45)
        self.assertEqual(intent.window, "this_week")
        self.assertEqual(intent.attendees, ["priya", "marco"])

    def test_hour_duration(self):
        intent = regex_intent("book an hour with Dana tomorrow", NAMES)
        self.assertEqual(intent.duration_minutes, 60)
        self.assertEqual(intent.window, "tomorrow")
        self.assertEqual(intent.attendees, ["dana"])

    def test_leave_request(self):
        intent = regex_intent("I'm on leave Thursday and Friday - find cover for my meetings", NAMES)
        self.assertEqual(intent.kind, "leave_coverage")
        self.assertEqual(intent.leave_days, ["thursday", "friday"])

    def test_unknown(self):
        intent = regex_intent("what a great day", NAMES)
        self.assertEqual(intent.kind, "unknown")

    def test_topic(self):
        intent = regex_intent("schedule 30 minutes with Priya about the Q3 launch", NAMES)
        self.assertEqual(intent.topic, "the q3 launch")


class ExtractFallbackTests(unittest.TestCase):
    @patch("cadence.llm.provider", return_value=None)
    def test_falls_back_to_regex_without_llm(self, _):
        intent = extract_intent("find 45 minutes with Priya this week", NAMES)
        self.assertEqual(intent.kind, "schedule_meeting")
        self.assertEqual(intent.attendees, ["priya"])


class WindowResolutionTests(unittest.TestCase):
    def test_this_week_from_saturday_starts_monday(self):
        window = resolve_meeting_window(Intent(window="this_week"), NOW)
        self.assertEqual(window.start.date().isoformat(), "2026-07-13")  # Monday
        self.assertEqual(window.end.date().isoformat(), "2026-07-17")    # Friday
        self.assertEqual(window.end.hour, 17)

    def test_tomorrow_from_saturday_is_monday(self):
        window = resolve_meeting_window(Intent(window="tomorrow"), NOW)
        self.assertEqual(window.start.date(), window.end.date())
        self.assertEqual(window.start.date().isoformat(), "2026-07-13")

    def test_next_week(self):
        window = resolve_meeting_window(Intent(window="next_week"), NOW)
        self.assertEqual(window.start.date().isoformat(), "2026-07-20")

    def test_leave_window_thursday_friday(self):
        intent = Intent(kind="leave_coverage", leave_days=["thursday", "friday"])
        window = resolve_leave_window(intent, NOW)
        self.assertEqual(window.start.date().isoformat(), "2026-07-16")
        self.assertEqual(window.end.date().isoformat(), "2026-07-17")

    def test_leave_window_empty(self):
        self.assertIsNone(resolve_leave_window(Intent(kind="leave_coverage"), NOW))


if __name__ == "__main__":
    unittest.main()
