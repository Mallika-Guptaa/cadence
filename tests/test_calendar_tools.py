import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp_server"))

from calendar_tools import CalendarStore, _to_ics


def make_calendar(email, name, events):
    return {"email": email, "name": name, "slack_name": name.lower(), "events": events}


EVENT = {
    "id": "SEED-1",
    "title": "Q3 review",
    "start": "2026-07-16T10:00:00-07:00",
    "end": "2026-07-16T10:45:00-07:00",
    "attendees": ["alex@t.demo", "priya@t.demo"],
    "kind": "meeting",
}


class CalendarStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.calendars = self.tmp / "calendars"
        self.events = self.tmp / "events"
        self.calendars.mkdir()
        for email, name, events in [
            ("alex@t.demo", "Alex", [dict(EVENT)]),
            ("priya@t.demo", "Priya", [dict(EVENT)]),
            ("marco@t.demo", "Marco", []),
        ]:
            path = self.calendars / f"{email.split('@')[0]}.json"
            path.write_text(json.dumps(make_calendar(email, name, events)))
        self.store = CalendarStore(self.calendars, self.events)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_list_users(self):
        emails = {u["email"] for u in self.store.list_users()}
        self.assertEqual(emails, {"alex@t.demo", "priya@t.demo", "marco@t.demo"})

    def test_availability_matches_fixtures(self):
        busy = self.store.get_availability(
            ["alex@t.demo", "marco@t.demo"],
            "2026-07-16T00:00:00-07:00",
            "2026-07-17T00:00:00-07:00",
        )
        self.assertEqual(len(busy["alex@t.demo"]), 1)
        self.assertEqual(busy["marco@t.demo"], [])

    def test_get_events_respects_window(self):
        inside = self.store.get_events(
            "alex@t.demo", "2026-07-16T00:00:00-07:00", "2026-07-17T00:00:00-07:00"
        )
        outside = self.store.get_events(
            "alex@t.demo", "2026-07-20T00:00:00-07:00", "2026-07-21T00:00:00-07:00"
        )
        self.assertEqual(len(inside), 1)
        self.assertEqual(outside, [])

    def test_create_event_writes_calendars_and_artifacts(self):
        event = self.store.create_event(
            "Sync", ["alex@t.demo", "marco@t.demo"],
            "2026-07-14T10:00:00-07:00", "2026-07-14T10:45:00-07:00", agenda="- topic",
        )
        self.assertEqual(event["id"], "EVT-1")
        marco = self.store.get_events(
            "marco@t.demo", "2026-07-14T00:00:00-07:00", "2026-07-15T00:00:00-07:00"
        )
        self.assertEqual(marco[0]["title"], "Sync")
        self.assertTrue((self.events / "EVT-1.json").exists())
        ics = (self.events / "EVT-1.ics").read_text()
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("SUMMARY:Sync", ics)
        self.assertIn("DTSTART:20260714T100000", ics)

    def test_event_ids_increment(self):
        first = self.store.create_event(
            "A", ["alex@t.demo"], "2026-07-14T10:00:00-07:00", "2026-07-14T10:30:00-07:00"
        )
        second = self.store.create_event(
            "B", ["alex@t.demo"], "2026-07-14T11:00:00-07:00", "2026-07-14T11:30:00-07:00"
        )
        self.assertEqual((first["id"], second["id"]), ("EVT-1", "EVT-2"))

    def test_reassign_swaps_attendee_and_moves_event(self):
        updated = self.store.reassign_event(
            "SEED-1", "alex@t.demo", "marco@t.demo", note="handover"
        )
        self.assertIn("marco@t.demo", updated["attendees"])
        self.assertNotIn("alex@t.demo", updated["attendees"])
        alex_events = self.store.get_events(
            "alex@t.demo", "2026-07-16T00:00:00-07:00", "2026-07-17T00:00:00-07:00"
        )
        marco_events = self.store.get_events(
            "marco@t.demo", "2026-07-16T00:00:00-07:00", "2026-07-17T00:00:00-07:00"
        )
        self.assertEqual(alex_events, [])
        self.assertEqual(marco_events[0]["id"], "SEED-1")
        self.assertEqual(marco_events[0]["handover_note"], "handover")

    def test_reassign_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            self.store.reassign_event("EVT-999", "alex@t.demo", "marco@t.demo")

    def test_reassign_non_attendee_raises(self):
        with self.assertRaises(ValueError):
            self.store.reassign_event("SEED-1", "marco@t.demo", "alex@t.demo")

    def test_record_leave(self):
        leave = self.store.record_leave(
            "alex@t.demo", "2026-07-16T00:00:00-07:00", "2026-07-18T00:00:00-07:00"
        )
        self.assertEqual(leave["kind"], "leave")
        busy = self.store.get_availability(
            ["alex@t.demo"], "2026-07-16T00:00:00-07:00", "2026-07-17T00:00:00-07:00"
        )
        titles = {b["title"] for b in busy["alex@t.demo"]}
        self.assertIn("On leave", titles)

    def test_unknown_calendar_raises(self):
        with self.assertRaises(ValueError):
            self.store.get_events("nobody@t.demo", "2026-07-16T00:00:00-07:00", "2026-07-17T00:00:00-07:00")


class IcsTests(unittest.TestCase):
    def test_ics_contains_attendees_and_description(self):
        event = dict(EVENT, agenda="line1\nline2")
        ics = _to_ics(event)
        self.assertIn("ATTENDEE:mailto:alex@t.demo", ics)
        self.assertIn("DESCRIPTION:line1\\nline2", ics)


if __name__ == "__main__":
    unittest.main()
