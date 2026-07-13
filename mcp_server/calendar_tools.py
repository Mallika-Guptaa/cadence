"""Cadence calendar MCP server.

Exposes team-calendar operations as MCP tools over stdio. The backend is a
JSON file store (mcp_server/calendars/*.json) so the demo is self-contained;
swapping in Google Calendar means reimplementing CalendarStore only — the tool
contract stays the same.

Run standalone checks with:  python mcp_server/calendar_tools.py --selftest
(As an MCP server this process communicates on stdout — all human-facing logs
go to stderr.)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CALENDARS_DIR = ROOT / "calendars"
EVENTS_DIR = ROOT / "events"
TASKS_DIR = ROOT / "tasks"
PROJECTS_DIR = ROOT / "projects"
CHANGELOG_PATH = ROOT / "changelog" / "CHANGELOG.md"
TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))


def log(message: str) -> None:
    print(f"[cadence-mcp] {message}", file=sys.stderr, flush=True)


class CalendarStore:
    """JSON-backed team calendar. One file per person, events inline."""

    def __init__(self, calendars_dir: Path = CALENDARS_DIR, events_dir: Path = EVENTS_DIR):
        self.calendars_dir = calendars_dir
        self.events_dir = events_dir
        self.events_dir.mkdir(parents=True, exist_ok=True)

    # -- persistence ---------------------------------------------------------

    def _load_all(self) -> dict[str, dict]:
        calendars = {}
        for path in sorted(self.calendars_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            calendars[data["email"]] = data
        return calendars

    def _save(self, calendar: dict) -> None:
        stem = calendar["email"].split("@")[0]
        path = self.calendars_dir / f"{stem}.json"
        path.write_text(json.dumps(calendar, indent=2), encoding="utf-8")

    def _next_id(self, prefix: str) -> str:
        highest = 0
        for cal in self._load_all().values():
            for event in cal["events"]:
                if event["id"].startswith(prefix + "-"):
                    highest = max(highest, int(event["id"].split("-")[1]))
        return f"{prefix}-{highest + 1}"

    # -- operations ----------------------------------------------------------

    def list_users(self) -> list[dict]:
        return [
            {"email": c["email"], "name": c["name"], "slack_name": c.get("slack_name", "")}
            for c in self._load_all().values()
        ]

    def get_availability(self, emails: list[str], start: str, end: str) -> dict[str, list[dict]]:
        window_start, window_end = _parse(start), _parse(end)
        calendars = self._load_all()
        busy: dict[str, list[dict]] = {}
        for email in emails:
            cal = calendars.get(email)
            if cal is None:
                raise ValueError(f"no calendar for {email}")
            busy[email] = [
                {"start": e["start"], "end": e["end"], "title": e["title"]}
                for e in cal["events"]
                if _parse(e["start"]) < window_end and _parse(e["end"]) > window_start
            ]
        return busy

    def get_events(self, email: str, start: str, end: str) -> list[dict]:
        window_start, window_end = _parse(start), _parse(end)
        cal = self._load_all().get(email)
        if cal is None:
            raise ValueError(f"no calendar for {email}")
        return [
            e
            for e in cal["events"]
            if _parse(e["start"]) < window_end and _parse(e["end"]) > window_start
        ]

    def create_event(
        self,
        title: str,
        attendees: list[str],
        start: str,
        end: str,
        agenda: str = "",
    ) -> dict:
        event_id = self._next_id("EVT")
        event = {
            "id": event_id,
            "title": title,
            "start": start,
            "end": end,
            "attendees": attendees,
            "agenda": agenda,
            "kind": "meeting",
        }
        calendars = self._load_all()
        for email in attendees:
            cal = calendars.get(email)
            if cal is None:
                raise ValueError(f"no calendar for {email}")
            cal["events"].append(event)
            self._save(cal)
        self._write_artifacts(event)
        log(f"{event_id} booked: '{title}' {start} -> {end} attendees={','.join(attendees)}")
        return event

    def reassign_event(self, event_id: str, from_email: str, to_email: str, note: str = "") -> dict:
        calendars = self._load_all()
        found = None
        for cal in calendars.values():
            for event in cal["events"]:
                if event["id"] == event_id:
                    found = event
                    break
        if found is None:
            raise ValueError(f"event {event_id} not found")
        if from_email not in found["attendees"]:
            raise ValueError(f"{from_email} is not an attendee of {event_id}")

        updated = dict(found)
        updated["attendees"] = [to_email if a == from_email else a for a in found["attendees"]]
        if note:
            updated["handover_note"] = note

        for cal in calendars.values():
            cal["events"] = [updated if e["id"] == event_id else e for e in cal["events"]]
        # move the event between personal calendars
        if not any(e["id"] == event_id for e in calendars[to_email]["events"]):
            calendars[to_email]["events"].append(updated)
        calendars[from_email]["events"] = [
            e for e in calendars[from_email]["events"] if e["id"] != event_id
        ]
        for cal in calendars.values():
            self._save(cal)
        self._write_artifacts(updated)
        log(f"{event_id} reassigned: {from_email} -> {to_email} ('{updated['title']}')")
        return updated

    def record_leave(self, email: str, start: str, end: str) -> dict:
        leave_id = self._next_id("LVE")
        event = {
            "id": leave_id,
            "title": "On leave",
            "start": start,
            "end": end,
            "attendees": [email],
            "kind": "leave",
        }
        calendars = self._load_all()
        cal = calendars.get(email)
        if cal is None:
            raise ValueError(f"no calendar for {email}")
        cal["events"].append(event)
        self._save(cal)
        log(f"{leave_id} recorded: {email} on leave {start} -> {end}")
        return event

    # -- artifacts -----------------------------------------------------------

    def _write_artifacts(self, event: dict) -> None:
        json_path = self.events_dir / f"{event['id']}.json"
        json_path.write_text(json.dumps(event, indent=2), encoding="utf-8")
        ics_path = self.events_dir / f"{event['id']}.ics"
        ics_path.write_text(_to_ics(event), encoding="utf-8")


def _parse(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def _ics_stamp(iso: str) -> str:
    return _parse(iso).strftime("%Y%m%dT%H%M%S")


def _to_ics(event: dict) -> str:
    attendees = "\r\n".join(f"ATTENDEE:mailto:{a}" for a in event["attendees"])
    description = (event.get("agenda") or event.get("handover_note") or "").replace("\n", "\\n")
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Cadence//Slack Agent//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{event['id']}@cadence\r\n"
        f"DTSTART:{_ics_stamp(event['start'])}\r\n"
        f"DTEND:{_ics_stamp(event['end'])}\r\n"
        f"SUMMARY:{event['title']}\r\n"
        f"DESCRIPTION:{description}\r\n"
        f"{attendees}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


class WorkStore:
    """Task filing, project fixtures, and the changelog artifact."""

    def __init__(self, tasks_dir: Path = TASKS_DIR, projects_dir: Path = PROJECTS_DIR,
                 changelog_path: Path = CHANGELOG_PATH):
        self.tasks_dir = tasks_dir
        self.projects_dir = projects_dir
        self.changelog_path = changelog_path
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.changelog_path.parent.mkdir(parents=True, exist_ok=True)

    def create_task(self, title: str, owner: str = "", due: str = "", source: str = "") -> dict:
        highest = 0
        for path in self.tasks_dir.glob("TASK-*.json"):
            try:
                highest = max(highest, int(path.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue
        task = {
            "id": f"TASK-{highest + 1}",
            "title": title,
            "owner": owner,
            "due": due,
            "source": source,
            "status": "open",
        }
        (self.tasks_dir / f"{task['id']}.json").write_text(json.dumps(task, indent=2), encoding="utf-8")
        log(f"{task['id']} filed: '{title}' owner={owner or 'unassigned'} due={due or 'n/a'}")
        return task

    def get_project(self, name: str) -> dict:
        stem = name.strip().lower().replace(" ", "_")
        path = self.projects_dir / f"{stem}.json"
        if not path.exists():
            available = [p.stem for p in sorted(self.projects_dir.glob("*.json"))]
            raise ValueError(f"no project '{name}' in tracker; available: {', '.join(available) or 'none'}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_projects(self) -> list[str]:
        return [p.stem for p in sorted(self.projects_dir.glob("*.json"))]

    def publish_changelog(self, markdown: str, version: str = "") -> dict:
        existing = self.changelog_path.read_text(encoding="utf-8") if self.changelog_path.exists() else "# Changelog\n"
        heading = f"\n## {version or 'Unreleased'}\n\n"
        self.changelog_path.write_text(existing + heading + markdown.strip() + "\n", encoding="utf-8")
        log(f"changelog updated ({version or 'Unreleased'}): {self.changelog_path}")
        return {"path": str(self.changelog_path), "version": version or "Unreleased"}


# -- MCP wiring ---------------------------------------------------------------

def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("cadence-calendar")
    store = CalendarStore()

    @mcp.tool()
    def list_users() -> list[dict]:
        """List everyone in the team directory with their calendar email."""
        return store.list_users()

    @mcp.tool()
    def get_availability(emails: list[str], start: str, end: str) -> dict:
        """Busy blocks per person between two ISO datetimes."""
        return store.get_availability(emails, start, end)

    @mcp.tool()
    def get_events(email: str, start: str, end: str) -> list[dict]:
        """One person's calendar events between two ISO datetimes."""
        return store.get_events(email, start, end)

    @mcp.tool()
    def create_event(title: str, attendees: list[str], start: str, end: str, agenda: str = "") -> dict:
        """Book a meeting on every attendee's calendar; returns the event with its id."""
        return store.create_event(title, attendees, start, end, agenda)

    @mcp.tool()
    def reassign_event(event_id: str, from_email: str, to_email: str, note: str = "") -> dict:
        """Hand an event over from one person to another (leave coverage)."""
        return store.reassign_event(event_id, from_email, to_email, note)

    @mcp.tool()
    def record_leave(email: str, start: str, end: str) -> dict:
        """Block out a leave window on someone's calendar."""
        return store.record_leave(email, start, end)

    work = WorkStore()

    @mcp.tool()
    def create_task(title: str, owner: str = "", due: str = "", source: str = "") -> dict:
        """File a follow-up task in the task tracker (e.g. an overdue promise)."""
        return work.create_task(title, owner, due, source)

    @mcp.tool()
    def get_project(name: str) -> dict:
        """Fetch a project's tracker record: tickets, statuses, owners, risks."""
        return work.get_project(name)

    @mcp.tool()
    def list_projects() -> list[str]:
        """List project names known to the tracker."""
        return work.list_projects()

    @mcp.tool()
    def publish_changelog(markdown: str, version: str = "") -> dict:
        """Append a release-notes section to the published changelog file."""
        return work.publish_changelog(markdown, version)

    return mcp


def _selftest() -> None:
    store = CalendarStore()
    users = store.list_users()
    log(f"selftest: {len(users)} users -> {', '.join(u['email'] for u in users)}")
    if not users:
        log("selftest: no calendars found — run scripts/gen_calendars.py first")
        sys.exit(1)
    emails = [u["email"] for u in users]
    sample = store.get_availability(emails[:2], "2020-01-01T00:00:00", "2030-01-01T00:00:00")
    for email, busy in sample.items():
        log(f"selftest: {email} has {len(busy)} events on file")
    log("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        build_server().run()
