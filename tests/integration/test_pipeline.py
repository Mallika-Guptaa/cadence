"""End-to-end pipeline check without Slack: intent -> MCP -> engine -> cards.

    PYTHONPATH=src .venv/bin/python tests/integration/test_pipeline.py

Uses the real MCP server subprocess and the demo calendars; regenerates the
fixtures afterwards so repeated runs stay deterministic.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cadence import slack_app  # noqa: E402


class Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

    @property
    def blocks(self):
        return self.calls[-1].get("blocks", [])


def main() -> None:
    # --- Flow 1: meeting negotiation -------------------------------------
    say = Capture()
    slack_app.handle_request("Find 45 minutes for me, Priya and Marco this week", say)
    actions = next(b for b in say.blocks if b.get("type") == "actions")
    buttons = actions["elements"]
    assert len(buttons) == 3, f"expected 3 slot buttons, got {len(buttons)}"
    payload = json.loads(buttons[0]["value"])
    assert set(payload["emails"]) == {
        "alex@cadence.demo", "priya@cadence.demo", "marco@cadence.demo"
    }, payload["emails"]
    print(f"flow 1 OK: 3 slots proposed, first = {payload['start']}")

    # book the first slot straight through MCP (what the button handler does)
    event = slack_app.mcp().call_tool(
        "create_event",
        {"title": "Team sync", "attendees": payload["emails"], "start": payload["start"], "end": payload["end"]},
    )
    assert event["id"].startswith("EVT-")
    assert (ROOT / "mcp_server" / "events" / f"{event['id']}.ics").exists()
    print(f"flow 1 OK: booked {event['id']}, .ics written")

    # --- Flow 2: leave coverage ------------------------------------------
    say = Capture()
    slack_app.handle_request("I'm on leave Thursday and Friday — find cover for my meetings", say)
    rows = [b for b in say.blocks if b.get("accessory")]
    warnings = [b for b in say.blocks if "needs manual cover" in str(b)]
    texts = str(say.blocks)
    assert "Q3 launch planning review" in texts, "Q3 review missing from coverage plan"
    assert "on leave that day" in texts, "expected Marco to be shown as skipped (on leave)"
    assert len(rows) >= 2, f"expected >=2 assignable rows, got {len(rows)}"
    assert len(warnings) == 1, f"expected exactly 1 manual-cover row (onboarding), got {len(warnings)}"

    by_title = {json.loads(r["accessory"]["value"])["title"]: json.loads(r["accessory"]["value"]) for r in rows}
    q3 = by_title.get("Q3 launch planning review")
    # Marco is OOO Thursday -> Dana covers the Q3 review, Marco explicitly skipped
    assert q3 and q3["to_email"] == "dana@cadence.demo", f"expected Dana to cover Q3 review (Marco on leave): {q3}"
    standup = by_title.get("Payments migration standup")
    assert standup and standup["to_email"] == "priya@cadence.demo", f"expected Priya to cover standup: {standup}"
    print("flow 2 OK: leave-aware coverage (Marco skipped OOO -> Dana covers Q3, Priya covers standup, onboarding manual)")

    # reassign one through MCP (what the Assign button does)
    updated = slack_app.mcp().call_tool(
        "reassign_event",
        {"event_id": q3["event_id"], "from_email": "alex@cadence.demo",
         "to_email": q3["to_email"], "note": "handover"},
    )
    assert "dana@cadence.demo" in updated["attendees"]
    print(f"flow 2 OK: {q3['event_id']} reassigned to Dana")

    print("PIPELINE TEST PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        if slack_app._mcp is not None:
            slack_app._mcp.close()
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "gen_calendars.py")],
            check=True, capture_output=True,
        )
        for stray in (ROOT / "mcp_server" / "events").glob("EVT-*"):
            stray.unlink()
        print("fixtures regenerated")
