"""Tests for the Project Status Autopilot feature (no network, no LLM)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cadence.features import FeatureContext, status
from cadence.store import Store

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 7, 10, 9, 0, tzinfo=TZ)

PROJECTS = [{"name": "Atlas"}, {"name": "payments-revamp"}]
ATLAS = {
    "name": "Atlas",
    "description": "Mobile app rewrite for offline sync and faster onboarding",
    "target_date": "2026-08-15",
    "owner": "Priya",
    "health": "at_risk",
    "tickets": [
        {"id": "ATL-1", "title": "Auth flow", "status": "done", "owner": "Marco", "updated": "2026-07-01"},
        {"id": "ATL-2", "title": "Sync engine", "status": "in_progress", "owner": "Priya", "updated": "2026-07-08"},
        {"id": "ATL-3", "title": "Push notifications", "status": "blocked", "owner": "Sam",
         "updated": "2026-07-09", "blocker": "waiting on APNs cert from IT"},
    ],
    "risks": ["App store review may slip past target", "Only one engineer knows the sync engine"],
}
PAYMENTS = {
    "name": "payments-revamp",
    "description": "Rebuild checkout on the new billing service",
    "target_date": "2026-09-01",
    "owner": "Marco",
    "health": "on_track",
    "tickets": [],
    "risks": [],
}


class StubMcp:
    def __init__(self, projects=PROJECTS, data=None, fail_tool=None):
        self.projects = projects
        self.data = data if data is not None else {"Atlas": ATLAS, "payments-revamp": PAYMENTS}
        self.fail_tool = fail_tool
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == self.fail_tool:
            raise RuntimeError("tracker exploded")
        if name == "list_projects":
            return self.projects
        if name == "get_project":
            return self.data.get(arguments.get("name"))
        raise AssertionError(f"unexpected tool {name}")


class FakeSlackClient:
    def conversations_list(self, **kwargs):
        return {"channels": []}

    def chat_getPermalink(self, channel, message_ts):
        return {"permalink": f"https://slack.test/{channel}/{message_ts}"}


def flat(blocks):
    return json.dumps(blocks)


class StatusFeatureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "test.db")

    def ctx(self, mcp=None, client=None):
        stub = mcp if mcp is not None else StubMcp()
        return stub, FeatureContext(store=self.store, mcp=lambda: stub, client=client, now=NOW)

    def test_status_of_extracts_name_and_fetches(self):
        stub, ctx = self.ctx()
        blocks, text = status.handle("what's the status of Atlas?", ctx)
        self.assertIn(("get_project", {"name": "Atlas"}), stub.calls)
        self.assertEqual(blocks[0]["type"], "header")
        self.assertEqual(blocks[0]["text"]["text"], "Project Atlas")
        self.assertIn("Atlas", text)

    def test_how_is_project_resolves_via_fuzzy_match(self):
        stub, ctx = self.ctx()
        status.handle("how is project atlas doing?", ctx)
        self.assertIn(("list_projects", {}), stub.calls)
        self.assertIn(("get_project", {"name": "Atlas"}), stub.calls)

    def test_status_update_on_hyphenated_name(self):
        stub, ctx = self.ctx()
        blocks, _ = status.handle("status update on Payments Revamp", ctx)
        self.assertIn(("get_project", {"name": "payments-revamp"}), stub.calls)
        self.assertIn("payments-revamp", flat(blocks))

    def test_unknown_project_lists_available(self):
        stub, ctx = self.ctx()
        blocks, text = status.handle("status of Zeppelin", ctx)
        rendered = flat(blocks)
        self.assertIn("couldn't find", rendered)
        self.assertIn("Atlas", rendered)
        self.assertIn("payments-revamp", rendered)
        self.assertNotIn("get_project", [name for name, _ in stub.calls])
        self.assertIn("Zeppelin", text)

    def test_ambiguous_request_asks_which_project(self):
        stub, ctx = self.ctx()
        blocks, text = status.handle("project status", ctx)
        self.assertIn("Which project?", text)
        self.assertIn("Available projects", flat(blocks))
        self.assertIn(("list_projects", {}), stub.calls)

    def test_card_contains_health_progress_blockers_risks(self):
        _, ctx = self.ctx()
        blocks, text = status.handle("status of Atlas", ctx)
        rendered = flat(blocks)
        self.assertIn(":large_yellow_circle:", rendered)
        self.assertIn("At risk", rendered)
        self.assertIn("1/3 done", rendered)
        self.assertIn("ATL-3", rendered)
        self.assertIn("waiting on APNs cert", rendered)
        self.assertIn("App store review may slip", rendered)
        self.assertIn("Sources: project tracker via MCP `get_project` + 0 Slack messages", rendered)
        self.assertIn("1 blocked", text)

    def test_slack_hits_appear_with_permalinks(self):
        ts = str(NOW.timestamp() - 3600)
        self.store.upsert_message(
            "C1", "#atlas", ts, "U1", "Sam", "Atlas sync engine hitting rate limits in staging"
        )
        _, ctx = self.ctx(client=FakeSlackClient())
        blocks, _ = status.handle("status of Atlas", ctx)
        rendered = flat(blocks)
        self.assertIn("Latest from Slack", rendered)
        self.assertIn(f"https://slack.test/C1/{ts}", rendered)
        self.assertIn("rate limits in staging", rendered)
        self.assertIn("+ 1 Slack messages", rendered)

    def test_get_project_failure_returns_error_card(self):
        _, ctx = self.ctx(mcp=StubMcp(fail_tool="get_project"))
        blocks, text = status.handle("status of Atlas", ctx)
        rendered = flat(blocks)
        self.assertIn(":warning:", rendered)
        self.assertIn("get_project", rendered)
        self.assertIn("tracker exploded", rendered)
        self.assertIn("Atlas", text)

    def test_broken_mcp_getter_never_raises(self):
        def boom():
            raise RuntimeError("no mcp")

        ctx = FeatureContext(store=self.store, mcp=boom, client=None, now=NOW)
        blocks, text = status.handle("status of Atlas", ctx)
        self.assertIn(":warning:", flat(blocks))
        self.assertIn("unavailable", text)

    def test_list_projects_failure_falls_back_to_extracted_name(self):
        stub = StubMcp(fail_tool="list_projects")
        _, ctx = self.ctx(mcp=stub)
        blocks, _ = status.handle("status of Atlas", ctx)
        self.assertIn(("get_project", {"name": "Atlas"}), stub.calls)
        self.assertEqual(blocks[0]["text"]["text"], "Project Atlas")


if __name__ == "__main__":
    unittest.main()
