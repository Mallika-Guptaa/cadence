import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cadence import block_kit
from cadence.slots import CoverageProposal, Interval

TZ = ZoneInfo("America/Los_Angeles")


def dt(day, hour, minute=0):
    return datetime(2026, 7, 13 + day, hour, minute, tzinfo=TZ)


class SlotCardTests(unittest.TestCase):
    def test_three_slot_buttons_with_payloads(self):
        slots = [Interval(dt(0, 12), dt(0, 12, 45)), Interval(dt(1, 10), dt(1, 10, 45)), Interval(dt(2, 11), dt(2, 11, 45))]
        blocks = block_kit.slot_card(slots, ["You", "Priya"], ["a@d", "p@d"], 45, "q3")
        actions = next(b for b in blocks if b["type"] == "actions")
        self.assertEqual(len(actions["elements"]), 3)
        for i, button in enumerate(actions["elements"]):
            self.assertEqual(button["action_id"], f"book_slot_{i}")
            payload = json.loads(button["value"])
            self.assertEqual(payload["emails"], ["a@d", "p@d"])
            self.assertIn("start", payload)
        self.assertEqual(actions["elements"][0]["style"], "primary")
        self.assertNotIn("style", actions["elements"][1])

    def test_empty_slots_message(self):
        blocks = block_kit.slot_card([], ["You"], ["a@d"], 45, "")
        self.assertTrue(any("No mutual free slot" in str(b) for b in blocks))


class CoverageCardTests(unittest.TestCase):
    def _proposal(self, covered=True):
        event = {"id": "SEED-4", "title": "Q3 review", "start": "2026-07-16T10:00:00-07:00", "end": "2026-07-16T10:45:00-07:00", "attendees": ["a@d", "p@d"]}
        candidates = [{"email": "m@d", "co_attendance": 3, "busy_minutes": 60}] if covered else []
        return CoverageProposal(event=event, candidates=candidates)

    def test_row_per_meeting_with_assign_button(self):
        blocks = block_kit.coverage_card([self._proposal()], "Thu Jul 16 – Fri Jul 17")
        row = next(b for b in blocks if b.get("accessory"))
        self.assertEqual(row["accessory"]["action_id"], "assign_cover_0")
        payload = json.loads(row["accessory"]["value"])
        self.assertEqual(payload["event_id"], "SEED-4")
        self.assertEqual(payload["to_email"], "m@d")

    def test_manual_cover_flagged(self):
        blocks = block_kit.coverage_card([self._proposal(covered=False)], "Thu – Fri")
        self.assertTrue(any("needs manual cover" in str(b) for b in blocks))

    def test_no_meetings(self):
        blocks = block_kit.coverage_card([], "Thu – Fri")
        self.assertTrue(any("No meetings need cover" in str(b) for b in blocks))


class ConfirmationTests(unittest.TestCase):
    def test_booking_confirmation_has_footer_trace(self):
        event = {"id": "EVT-1", "title": "Q3 sync", "start": "2026-07-13T12:00:00-07:00", "attendees": ["alex@d", "priya@d"]}
        blocks = block_kit.booking_confirmation(event, "- agenda", "Context: Real-Time Search API (3 results)")
        footer = blocks[-1]["elements"][0]["text"]
        self.assertIn("Real-Time Search API", footer)
        self.assertIn("create_event", footer)

    def test_coverage_confirmation(self):
        event = {"id": "SEED-4", "title": "Q3 review", "start": "2026-07-16T10:00:00-07:00"}
        blocks = block_kit.coverage_confirmation(event, "marco@d", "- brief", "Context: channel history (5)")
        self.assertIn("Marco", str(blocks[0]))
        self.assertIn("reassign_event", blocks[-1]["elements"][0]["text"])


if __name__ == "__main__":
    unittest.main()
