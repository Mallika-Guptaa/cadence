import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cadence.slots import (
    Interval,
    find_free_slots,
    free_within,
    match_coverage,
    merge_busy,
    total_busy_minutes,
    working_windows,
)

TZ = ZoneInfo("America/Los_Angeles")


def dt(day, hour, minute=0):
    # Monday 2026-07-13 + offset days — fixed dates, never wall clock
    return datetime(2026, 7, 13 + day, hour, minute, tzinfo=TZ)


class MergeBusyTests(unittest.TestCase):
    def test_overlapping_blocks_merge(self):
        merged = merge_busy(
            [Interval(dt(0, 9), dt(0, 10)), Interval(dt(0, 9, 30), dt(0, 11))]
        )
        self.assertEqual(merged, [Interval(dt(0, 9), dt(0, 11))])

    def test_adjacent_blocks_merge(self):
        merged = merge_busy(
            [Interval(dt(0, 9), dt(0, 10)), Interval(dt(0, 10), dt(0, 11))]
        )
        self.assertEqual(merged, [Interval(dt(0, 9), dt(0, 11))])

    def test_disjoint_blocks_stay_separate(self):
        merged = merge_busy(
            [Interval(dt(0, 14), dt(0, 15)), Interval(dt(0, 9), dt(0, 10))]
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].start, dt(0, 9))

    def test_empty(self):
        self.assertEqual(merge_busy([]), [])


class WorkingWindowsTests(unittest.TestCase):
    def test_weekend_excluded(self):
        # Mon 2026-07-13 .. Sun 2026-07-19 -> 5 business-day windows
        windows = working_windows(Interval(dt(0, 0), dt(6, 23)))
        self.assertEqual(len(windows), 5)
        self.assertTrue(all(w.start.time().hour == 9 for w in windows))

    def test_window_clamped_to_working_hours(self):
        windows = working_windows(Interval(dt(0, 11), dt(0, 23)))
        self.assertEqual(windows, [Interval(dt(0, 11), dt(0, 17))])


class FreeWithinTests(unittest.TestCase):
    def test_busy_splits_window(self):
        free = free_within(
            Interval(dt(0, 9), dt(0, 17)), [Interval(dt(0, 10), dt(0, 11))]
        )
        self.assertEqual(free, [Interval(dt(0, 9), dt(0, 10)), Interval(dt(0, 11), dt(0, 17))])

    def test_busy_covering_whole_window(self):
        free = free_within(
            Interval(dt(0, 9), dt(0, 17)), [Interval(dt(0, 8), dt(0, 18))]
        )
        self.assertEqual(free, [])

    def test_busy_at_boundary(self):
        free = free_within(
            Interval(dt(0, 9), dt(0, 17)), [Interval(dt(0, 9), dt(0, 10))]
        )
        self.assertEqual(free, [Interval(dt(0, 10), dt(0, 17))])


class FindFreeSlotsTests(unittest.TestCase):
    def test_slot_must_fit_inside_working_hours(self):
        # busy until 16:15 -> grid aligns to 16:30, but 16:30 + 45m > 17:00 -> no slot
        busy = {"a": [Interval(dt(0, 9), dt(0, 16, 15))]}
        slots = find_free_slots(busy, Interval(dt(0, 9), dt(0, 17)), 45, max_slots=1)
        self.assertEqual(slots, [])

    def test_slot_fits_exactly_at_window_edge(self):
        # busy until 16:15 -> 16:30 + 30m = 17:00 exactly -> allowed
        busy = {"a": [Interval(dt(0, 9), dt(0, 16, 15))]}
        slots = find_free_slots(busy, Interval(dt(0, 9), dt(0, 17)), 30, max_slots=1)
        self.assertEqual(slots, [Interval(dt(0, 16, 30), dt(0, 17))])

    def test_no_mutual_slot_returns_empty(self):
        busy = {
            "a": [Interval(dt(0, 9), dt(0, 13))],
            "b": [Interval(dt(0, 13), dt(0, 17))],
        }
        slots = find_free_slots(busy, Interval(dt(0, 9), dt(0, 17)), 45)
        self.assertEqual(slots, [])

    def test_prefers_sooner_day_then_mid_morning(self):
        busy = {"a": [Interval(dt(0, 9), dt(0, 12))]}  # day 0 free only after 12
        slots = find_free_slots(busy, Interval(dt(0, 9), dt(1, 17)), 45, max_slots=3)
        self.assertEqual(slots[0].start, dt(0, 12))  # day 0 beats day 1 despite worse hour
        self.assertEqual(slots[1].start, dt(1, 10))  # day 1's mid-morning ideal
        self.assertEqual(len({s.start.date() for s in slots[:2]}), 2)  # diversified days

    def test_ranking_stable(self):
        busy = {"a": []}
        window = Interval(dt(0, 9), dt(2, 17))
        first = find_free_slots(busy, window, 30)
        second = find_free_slots(busy, window, 30)
        self.assertEqual(first, second)

    def test_all_people_considered(self):
        busy = {
            "a": [],
            "b": [Interval(dt(0, 10), dt(0, 10, 30))],
        }
        slots = find_free_slots(busy, Interval(dt(0, 9), dt(0, 17)), 30, max_slots=3)
        for slot in slots:
            self.assertFalse(slot.overlaps(Interval(dt(0, 10), dt(0, 10, 30))))


class MatchCoverageTests(unittest.TestCase):
    def _event(self, title, start, end, attendees):
        return {"id": "SEED-1", "title": title, "start": start, "end": end, "attendees": attendees}

    def test_free_candidate_proposed(self):
        events = [self._event("Q3 review", dt(3, 10), dt(3, 10, 45), ["alex", "priya"])]
        proposals = match_coverage(
            "alex",
            Interval(dt(3, 0), dt(4, 23)),
            events,
            {"marco": [], "dana": [Interval(dt(3, 10), dt(3, 11))]},
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].best["email"], "marco")

    def test_existing_attendee_not_proposed(self):
        events = [self._event("Q3 review", dt(3, 10), dt(3, 10, 45), ["alex", "priya"])]
        proposals = match_coverage(
            "alex", Interval(dt(3, 0), dt(4, 23)), events, {"priya": [], "marco": []}
        )
        self.assertEqual([c["email"] for c in proposals[0].candidates], ["marco"])

    def test_solo_focus_block_excluded(self):
        events = [self._event("Focus block", dt(3, 13), dt(3, 15), ["alex"])]
        proposals = match_coverage("alex", Interval(dt(3, 0), dt(4, 23)), events, {"marco": []})
        self.assertEqual(proposals, [])

    def test_nobody_free_flags_manual_cover(self):
        events = [self._event("Onboarding", dt(4, 14), dt(4, 14, 30), ["alex", "dana"])]
        proposals = match_coverage(
            "alex",
            Interval(dt(3, 0), dt(4, 23)),
            events,
            {
                "priya": [Interval(dt(4, 14), dt(4, 15))],
                "marco": [Interval(dt(4, 13), dt(4, 16))],
            },
        )
        self.assertTrue(proposals[0].needs_manual_cover)

    def test_ranking_prefers_co_attendance_then_load(self):
        events = [self._event("Standup", dt(4, 9, 30), dt(4, 9, 45), ["alex", "intern"])]
        proposals = match_coverage(
            "alex",
            Interval(dt(3, 0), dt(4, 23)),
            events,
            {"priya": [], "marco": [], "dana": [Interval(dt(0, 9), dt(0, 17))]},
            co_attendance={"marco": 3, "priya": 1, "dana": 5},
        )
        ranked = [c["email"] for c in proposals[0].candidates]
        self.assertEqual(ranked[0], "dana")   # highest co-attendance wins despite load
        self.assertEqual(ranked[1], "marco")

    def test_event_outside_leave_window_ignored(self):
        events = [self._event("Early meeting", dt(0, 10), dt(0, 11), ["alex", "priya"])]
        proposals = match_coverage("alex", Interval(dt(3, 0), dt(4, 23)), events, {"marco": []})
        self.assertEqual(proposals, [])


class TotalBusyMinutesTests(unittest.TestCase):
    def test_overlaps_not_double_counted(self):
        minutes = total_busy_minutes(
            [Interval(dt(0, 9), dt(0, 10)), Interval(dt(0, 9, 30), dt(0, 10, 30))]
        )
        self.assertEqual(minutes, 90)


if __name__ == "__main__":
    unittest.main()


class LeaveAwareCoverageTests(unittest.TestCase):
    def _event(self, title, start, end, attendees):
        return {"id": "E1", "title": title, "start": start, "end": end, "attendees": attendees}

    def test_candidate_on_leave_is_skipped_and_recorded(self):
        # Q3 review needs cover; marco is free at the time BUT on leave that day -> skip
        events = [self._event("Q3 review", dt(3, 10), dt(3, 10, 45), ["alex", "priya"])]
        proposals = match_coverage(
            "alex", Interval(dt(3, 0), dt(4, 23)), events,
            busy_by_candidate={"marco": [], "dana": []},
            co_attendance={"marco": 5, "dana": 1},  # marco preferred, but he's out
            leave_by_candidate={"marco": [Interval(dt(3, 0), dt(3, 23, 59))]},
        )
        p = proposals[0]
        self.assertEqual(p.best["email"], "dana")     # marco skipped despite higher co-attendance
        self.assertIn("marco", p.on_leave)
        self.assertNotIn("marco", [c["email"] for c in p.candidates])

    def test_all_candidates_on_leave_needs_manual(self):
        events = [self._event("Sync", dt(3, 10), dt(3, 11), ["alex", "priya"])]
        proposals = match_coverage(
            "alex", Interval(dt(3, 0), dt(4, 23)), events,
            busy_by_candidate={"marco": []},
            leave_by_candidate={"marco": [Interval(dt(3, 0), dt(3, 23, 59))]},
        )
        self.assertTrue(proposals[0].needs_manual_cover)
        self.assertEqual(proposals[0].on_leave, ["marco"])
