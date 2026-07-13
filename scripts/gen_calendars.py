"""Generate the demo team calendars, anchored to the next business week.

Re-run any time before a demo so all events sit in the near future:

    python scripts/gen_calendars.py

The week is crafted so the two flagship flows look obviously correct:
- "45 minutes with Priya and Marco this week" has clean mutual slots on the
  first three days.
- Alex's leave on days 4-5 contains: a meeting Marco should cover (free +
  works with Alex most), a standup Priya should cover, a solo focus block
  (needs no cover), and a call where nobody is free (honest "needs manual
  cover" row).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CALENDARS_DIR = ROOT / "mcp_server" / "calendars"
TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))

ALEX = "alex@cadence.demo"
PRIYA = "priya@cadence.demo"
MARCO = "marco@cadence.demo"
DANA = "dana@cadence.demo"


def next_business_days(count: int = 5) -> list:
    """The next `count` business days, starting tomorrow — the same block the
    scheduler treats as 'this week', so meetings live where the agent looks."""
    days = []
    day = datetime.now(TZ).date() + timedelta(days=1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _weekday_index(days: list, weekday: int, default: int) -> int:
    """Index in `days` whose weekday matches (0=Mon…4=Fri); fallback to default."""
    for i, d in enumerate(days):
        if d.weekday() == weekday:
            return i
    return default


def at(day, hour, minute=0) -> str:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ).isoformat()


def main() -> None:
    d = next_business_days(5)
    # place the leave-window meetings on the actual Thursday & Friday of the block
    # so "on leave Thursday and Friday" always lands on them
    thu, fri = _weekday_index(d, 3, 3), _weekday_index(d, 4, 4)
    early = [i for i in range(5) if i not in (thu, fri)]  # non-leave days for other meetings
    e0, e1, e2 = (early + [0, 1, 2])[:3]
    seed = 0

    def event(title, day, start_h, start_m, end_h, end_m, attendees):
        nonlocal seed
        seed += 1
        return {
            "id": f"SEED-{seed}",
            "title": title,
            "start": at(day, start_h, start_m),
            "end": at(day, end_h, end_m),
            "attendees": attendees,
            "kind": "meeting",
        }

    sprint = event("Sprint planning", d[e0], 9, 0, 10, 30, [ALEX, PRIYA, MARCO])
    standup1 = event("Team standup", d[e1], 9, 0, 9, 30, [ALEX, MARCO])
    design = event("Design review", d[e1], 11, 0, 12, 0, [ALEX, PRIYA])
    # leave-window meetings (Thursday + Friday)
    q3_review = event("Q3 launch planning review", d[thu], 10, 0, 10, 45, [ALEX, PRIYA])
    payments = event("Payments migration standup", d[fri], 9, 30, 9, 45, [ALEX, MARCO])
    onboarding = event("Customer onboarding call", d[fri], 14, 0, 14, 30, [ALEX, DANA])

    calendars = {
        ALEX: {
            "email": ALEX,
            "name": "Alex",
            "slack_name": "alex",
            "events": [
                sprint,
                event("1:1 with manager", d[e0], 14, 0, 15, 0, [ALEX]),
                standup1,
                design,
                q3_review,
                payments,
                onboarding,
            ],
        },
        PRIYA: {
            "email": PRIYA,
            "name": "Priya",
            "slack_name": "priya",
            "events": [
                sprint,
                event("Interview loop", d[e0], 11, 0, 12, 0, [PRIYA]),
                design,
                event("Roadmap workshop", d[e1], 14, 0, 15, 30, [PRIYA, MARCO]),
                q3_review,
                # busy Friday afternoon -> can't cover the onboarding call
                event("Analytics deep dive", d[fri], 14, 0, 15, 0, [PRIYA]),
            ],
        },
        MARCO: {
            "email": MARCO,
            "name": "Marco",
            "slack_name": "marco",
            "events": [
                sprint,
                event("Vendor call", d[e0], 13, 0, 14, 0, [MARCO]),
                standup1,
                event("Roadmap workshop", d[e1], 14, 0, 15, 30, [PRIYA, MARCO]),
                # Marco is OOO Thursday — coverage must skip him for the Q3 review
                {"id": "SEED-MARCO-OOO", "title": "OOO — personal day",
                 "start": at(d[thu], 0, 0), "end": at(d[thu], 23, 59),
                 "attendees": [MARCO], "kind": "leave"},
                payments,
                # busy Friday afternoon too -> onboarding has nobody free
                event("Code review block", d[fri], 14, 0, 15, 0, [MARCO]),
            ],
        },
        DANA: {
            "email": DANA,
            "name": "Dana",
            "slack_name": "dana",
            "events": [
                event("Data pipeline sync", d[e0], 10, 0, 11, 0, [DANA]),
                event("Metrics review", d[e1], 15, 0, 16, 0, [DANA]),
                event("Support rotation", d[e2], 13, 0, 14, 0, [DANA]),
                onboarding,
            ],
        },
    }

    CALENDARS_DIR.mkdir(parents=True, exist_ok=True)
    for cal in calendars.values():
        stem = cal["email"].split("@")[0]
        (CALENDARS_DIR / f"{stem}.json").write_text(json.dumps(cal, indent=2), encoding="utf-8")

    print(f"Wrote {len(calendars)} calendars to {CALENDARS_DIR}", file=sys.stderr)
    print(f"Demo week: {d[0]} .. {d[4]}  (leave demo: Thu {d[thu]} + Fri {d[fri]})", file=sys.stderr)


if __name__ == "__main__":
    main()
