"""Round-trip the real MCP server over stdio. Run manually:

    PYTHONPATH=src .venv/bin/python tests/integration/test_mcp_roundtrip.py

Spawns the actual server subprocess; uses (and mutates) the demo calendars,
so re-run scripts/gen_calendars.py afterwards if you want pristine fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cadence.mcp_client import McpCalendarClient


def main() -> None:
    client = McpCalendarClient(str(ROOT / "mcp_server" / "calendar_tools.py"))
    try:
        tools = client.list_tools()
        assert set(tools) >= {
            "list_users", "get_availability", "get_events",
            "create_event", "reassign_event", "record_leave",
        }, tools
        print(f"tools: {sorted(tools)}")

        users = client.call_tool("list_users", {})
        emails = [u["email"] for u in users]
        print(f"users: {emails}")
        assert "alex@cadence.demo" in emails

        events = client.call_tool(
            "get_events",
            {"email": "alex@cadence.demo", "start": "2020-01-01T00:00:00", "end": "2030-01-01T00:00:00"},
        )
        assert events, "alex should have seeded events"
        window_start, window_end = events[0]["start"], events[0]["end"]

        busy = client.call_tool(
            "get_availability",
            {"emails": emails, "start": window_start, "end": window_end},
        )
        assert set(busy.keys()) == set(emails)

        created = client.call_tool(
            "create_event",
            {
                "title": "MCP roundtrip check",
                "attendees": ["alex@cadence.demo", "dana@cadence.demo"],
                "start": "2030-06-01T10:00:00-07:00",
                "end": "2030-06-01T10:30:00-07:00",
                "agenda": "- verify roundtrip",
            },
        )
        event_id = created["id"]
        print(f"created: {event_id}")
        assert (ROOT / "mcp_server" / "events" / f"{event_id}.ics").exists()

        reassigned = client.call_tool(
            "reassign_event",
            {"event_id": event_id, "from_email": "alex@cadence.demo",
             "to_email": "marco@cadence.demo", "note": "roundtrip handover"},
        )
        assert "marco@cadence.demo" in reassigned["attendees"]
        print(f"reassigned: {event_id} -> marco")

        leave = client.call_tool(
            "record_leave",
            {"email": "alex@cadence.demo", "start": "2030-06-02T00:00:00-07:00",
             "end": "2030-06-04T00:00:00-07:00"},
        )
        assert leave["kind"] == "leave"
        print(f"leave: {leave['id']}")

        print("MCP ROUNDTRIP PASSED")
    finally:
        client.close()


if __name__ == "__main__":
    main()
