"""Request understanding: classify the ask and extract scheduling parameters.

One LLM call returns a strict schema; a regex parser covers the same ground so
the demo cannot die on an API hiccup. Window resolution is pure and takes an
explicit `now`, so it is unit-testable with fixed dates.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .llm import parse_structured
from .slots import WORK_END, Interval

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


FEATURE_KINDS = ("promises", "catch_me_up", "project_status", "who_knows", "release_notes", "deja_vu")


class Intent(BaseModel):
    kind: Literal[
        "schedule_meeting", "leave_coverage",
        "promises", "catch_me_up", "project_status", "who_knows", "release_notes", "deja_vu",
        "unknown",
    ] = "unknown"
    attendees: list[str] = Field(default_factory=list, description="First names mentioned, excluding the requester")
    duration_minutes: int = 30
    window: Literal["this_week", "tomorrow", "next_week"] = "this_week"
    leave_days: list[str] = Field(default_factory=list, description="Weekday names for the leave window")
    topic: str = ""


EXTRACTION_SYSTEM = """You extract intents from Slack messages for a workplace productivity agent.
Classify the request:
- schedule_meeting: find a time and book a meeting with people.
- leave_coverage: the user is going on leave/vacation/OOO and wants their meetings covered.
- promises: show/track commitments people made ("open promises", "what do I owe").
- catch_me_up: digest of what the user missed while away.
- project_status: status report on a named project.
- who_knows: find the right person for a topic ("who knows about X").
- release_notes: compile release notes / changelog from recent messages.
- deja_vu: check whether a topic was already discussed before.
- unknown: anything else.
Extract attendee first names (lowercase, excluding the requester), meeting duration in minutes
(default 30), the time window (this_week, tomorrow, or next_week), leave weekday names
(lowercase, for leave_coverage), and a short topic phrase if one is stated."""


def extract_intent(text: str, known_names: list[str]) -> Intent:
    hints = f"Known team members: {', '.join(known_names)}.\nMessage: {text}"
    result = parse_structured(EXTRACTION_SYSTEM, hints, Intent)
    if result is not None and result.kind != "unknown":
        result.attendees = [a.lower() for a in result.attendees]
        result.leave_days = [d.lower() for d in result.leave_days]
        return result
    return regex_intent(text, known_names)


def regex_intent(text: str, known_names: list[str]) -> Intent:
    lowered = text.lower()
    intent = Intent()

    if re.search(r"\b(leave|vacation|ooo|out of office|pto|find cover|cover for)\b", lowered):
        intent.kind = "leave_coverage"
    elif re.search(r"\b(find|schedule|book|set ?up|meet|minutes?|sync|time)\b", lowered):
        intent.kind = "schedule_meeting"

    match = re.search(r"(\d+)\s*(?:min|mins|minutes)", lowered)
    if match:
        intent.duration_minutes = int(match.group(1))
    elif re.search(r"\b(?:an?|1|one)\s*(?:hour|hr)\b", lowered):
        intent.duration_minutes = 60
    elif match := re.search(r"(\d+)\s*(?:hours|hrs)", lowered):
        intent.duration_minutes = int(match.group(1)) * 60

    if "tomorrow" in lowered:
        intent.window = "tomorrow"
    elif "next week" in lowered:
        intent.window = "next_week"

    intent.attendees = [n for n in known_names if re.search(rf"\b{re.escape(n.lower())}\b", lowered)]
    intent.leave_days = [d for d in WEEKDAYS if d in lowered]

    match = re.search(r"\babout\s+(.+?)(?:\.|$)", lowered)
    if match:
        intent.topic = match.group(1).strip()
    return intent


def resolve_meeting_window(intent: Intent, now: datetime) -> Interval:
    """Turn 'this week / tomorrow / next week' into a concrete datetime span."""
    tz = now.tzinfo
    if intent.window == "tomorrow":
        day = _next_business_day(now.date() + timedelta(days=1))
        return Interval(
            datetime.combine(day, time(0, 0), tzinfo=tz),
            datetime.combine(day, WORK_END, tzinfo=tz),
        )
    start_offset = 7 if intent.window == "next_week" else 1
    first = _next_business_day(now.date() + timedelta(days=start_offset))
    days, day = [], first
    while len(days) < 5:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return Interval(
        datetime.combine(days[0], time(0, 0), tzinfo=tz),
        datetime.combine(days[-1], WORK_END, tzinfo=tz),
    )


def resolve_leave_dates(intent: Intent, now: datetime) -> list:
    """Next occurrence of each named leave weekday (dates, sorted)."""
    if not intent.leave_days:
        return []
    targets = {WEEKDAYS.index(d) for d in intent.leave_days if d in WEEKDAYS}
    matched: list = []
    for offset in range(1, 15):
        day = now.date() + timedelta(days=offset)
        if day.weekday() in targets:
            matched.append(day)
        if len(matched) == len(targets):
            break
    return sorted(matched)


def resolve_leave_window(intent: Intent, now: datetime) -> Interval | None:
    """Span covering the named leave days. NOTE: for non-contiguous days the
    span includes working days in between — callers that enumerate affected
    events must filter with resolve_leave_dates()."""
    matched = resolve_leave_dates(intent, now)
    if not matched:
        return None
    tz = now.tzinfo
    return Interval(
        datetime.combine(matched[0], time(0, 0), tzinfo=tz),
        datetime.combine(matched[-1], time(23, 59), tzinfo=tz),
    )


def _next_business_day(day):
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day
