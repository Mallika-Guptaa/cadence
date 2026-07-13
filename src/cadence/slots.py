"""Deterministic scheduling core: free-slot intersection and leave-coverage matching.

Pure functions over datetime intervals — no I/O, no wall clock. Everything here
is unit-tested with fixed datetimes; the Slack layer and MCP tools supply real
data at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

WORK_START = time(9, 0)
WORK_END = time(17, 0)
SLOT_STEP_MINUTES = 30


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class CoverageProposal:
    """Coverage plan for one event in the leave window."""

    event: dict[str, Any]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    on_leave: list[str] = field(default_factory=list)  # colleagues skipped — they're out too

    @property
    def best(self) -> dict[str, Any] | None:
        return self.candidates[0] if self.candidates else None

    @property
    def needs_manual_cover(self) -> bool:
        return not self.candidates


def merge_busy(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping/adjacent busy intervals into a sorted minimal set."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: (iv.start, iv.end))
    merged = [ordered[0]]
    for iv in ordered[1:]:
        last = merged[-1]
        if iv.start <= last.end:
            if iv.end > last.end:
                merged[-1] = Interval(last.start, iv.end)
        else:
            merged.append(iv)
    return merged


def working_windows(window: Interval) -> list[Interval]:
    """Split a date window into per-day working-hour intervals (Mon-Fri)."""
    windows: list[Interval] = []
    day = window.start.date()
    while day <= window.end.date():
        if day.weekday() < 5:
            tz = window.start.tzinfo
            day_start = datetime.combine(day, WORK_START, tzinfo=tz)
            day_end = datetime.combine(day, WORK_END, tzinfo=tz)
            clipped_start = max(day_start, window.start)
            clipped_end = min(day_end, window.end)
            if clipped_start < clipped_end:
                windows.append(Interval(clipped_start, clipped_end))
        day = day + timedelta(days=1)
    return windows


def free_within(window: Interval, busy: list[Interval]) -> list[Interval]:
    """Subtract busy intervals from one working window."""
    free: list[Interval] = []
    cursor = window.start
    for iv in merge_busy(busy):
        if iv.end <= window.start or iv.start >= window.end:
            continue
        if iv.start > cursor:
            free.append(Interval(cursor, min(iv.start, window.end)))
        cursor = max(cursor, iv.end)
        if cursor >= window.end:
            break
    if cursor < window.end:
        free.append(Interval(cursor, window.end))
    return free


def _slot_score(slot: Interval, window_start: datetime) -> float:
    """Lower is better: sooner days first, mid-morning (10:00) preferred."""
    days_out = (slot.start.date() - window_start.date()).days
    ideal = datetime.combine(slot.start.date(), time(10, 0), tzinfo=slot.start.tzinfo)
    hours_from_ideal = abs((slot.start - ideal).total_seconds()) / 3600
    return days_out * 100 + hours_from_ideal


def find_free_slots(
    busy_by_person: dict[str, list[Interval]],
    window: Interval,
    duration_minutes: int,
    max_slots: int = 3,
) -> list[Interval]:
    """Top slots where every person is free, diversified across days."""
    all_busy = [iv for ivs in busy_by_person.values() for iv in ivs]
    candidates: list[Interval] = []
    for day_window in working_windows(window):
        for free in free_within(day_window, all_busy):
            start = free.start
            step = timedelta(minutes=SLOT_STEP_MINUTES)
            # align to the step grid
            if start.minute % SLOT_STEP_MINUTES:
                start += timedelta(minutes=SLOT_STEP_MINUTES - start.minute % SLOT_STEP_MINUTES)
            while start + timedelta(minutes=duration_minutes) <= free.end:
                candidates.append(Interval(start, start + timedelta(minutes=duration_minutes)))
                start += step

    candidates.sort(key=lambda s: _slot_score(s, window.start))

    picked: list[Interval] = []
    used_days: set = set()
    for slot in candidates:  # first pass: one slot per day
        if slot.start.date() not in used_days:
            picked.append(slot)
            used_days.add(slot.start.date())
        if len(picked) == max_slots:
            return picked
    for slot in candidates:  # second pass: fill remaining from best overall
        if slot not in picked:
            picked.append(slot)
        if len(picked) == max_slots:
            break
    return picked


def total_busy_minutes(busy: list[Interval]) -> int:
    return sum(iv.duration_minutes() for iv in merge_busy(busy))


def match_coverage(
    leaver_email: str,
    leave_window: Interval,
    leaver_events: list[dict[str, Any]],
    busy_by_candidate: dict[str, list[Interval]],
    co_attendance: dict[str, int] | None = None,
    leave_by_candidate: dict[str, list[Interval]] | None = None,
) -> list[CoverageProposal]:
    """Propose a substitute for each of the leaver's meetings in the window.

    Events are dicts with at least: id, title, start/end (datetime), attendees
    (list of emails). Solo events (no other attendee) don't need cover and are
    excluded. A candidate qualifies only if they are (a) not themselves on
    leave/OOO during the event and (b) otherwise free during it; ranking prefers
    people who already work with the leaver (co_attendance), then the
    least-loaded calendar, then name for stability. Colleagues skipped because
    they're also out are surfaced separately so the reason is visible.
    """
    co_attendance = co_attendance or {}
    leave_by_candidate = leave_by_candidate or {}
    proposals: list[CoverageProposal] = []
    for event in leaver_events:
        event_iv = Interval(event["start"], event["end"])
        if not event_iv.overlaps(leave_window):
            continue
        others = [a for a in event.get("attendees", []) if a != leaver_email]
        if not others:
            continue  # solo focus block — nothing to hand over
        candidates = []
        on_leave = []
        for email, busy in busy_by_candidate.items():
            if email == leaver_email or email in event.get("attendees", []):
                continue
            if any(iv.overlaps(event_iv) for iv in leave_by_candidate.get(email, [])):
                on_leave.append(email)  # they're out too — not viable, and say why
                continue
            if any(iv.overlaps(event_iv) for iv in busy):
                continue
            candidates.append(
                {
                    "email": email,
                    "co_attendance": co_attendance.get(email, 0),
                    "busy_minutes": total_busy_minutes(busy),
                }
            )
        candidates.sort(key=lambda c: (-c["co_attendance"], c["busy_minutes"], c["email"]))
        proposals.append(CoverageProposal(event=event, candidates=candidates, on_leave=on_leave))
    return proposals
