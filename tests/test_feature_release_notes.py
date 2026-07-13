"""Tests for the release_notes feature — no network, no LLM, no Slack client."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cadence.features import FeatureContext, release_notes
from cadence.store import Store

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=TZ)


class StubMcp:
    def __init__(self, result=None, error: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.result = {"path": "CHANGELOG.md"} if result is None else result
        self.error = error

    def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "test.db")
        self.mcp = StubMcp()
        no_llm = patch("cadence.llm.draft", return_value=None)
        no_llm.start()
        self.addCleanup(no_llm.stop)
        self._seq = 0

    def ctx(self, mcp: StubMcp | None = None) -> FeatureContext:
        stub = mcp if mcp is not None else self.mcp
        return FeatureContext(store=self.store, mcp=lambda: stub, client=None, now=NOW)

    def seed(self, text: str, days_ago: float = 1.0) -> None:
        self._seq += 1
        ts = f"{(NOW - timedelta(days=days_ago)).timestamp() + self._seq:.6f}"
        self.store.upsert_message("C1", "#eng", ts, "U1", "Alex", text)

    def test_categorization_buckets_messages(self):
        self.seed("shipped the new dashboard")
        self.seed("fixed the login timeout")
        self.seed("optimized search, results load faster now")
        self.seed("deprecated the v1 export API")
        release_notes.handle("compile the release notes", self.ctx())
        markdown = self.mcp.calls[0][1]["markdown"]
        for heading in ("### Added", "### Fixed", "### Improved", "### Removed"):
            self.assertIn(heading, markdown)
        self.assertIn("- Shipped the new dashboard", markdown)
        self.assertIn("- Deprecated the v1 export API", markdown)

    def test_first_matching_category_wins(self):
        self.seed("shipped a fix for the crash bug")
        release_notes.handle("changelog please", self.ctx())
        markdown = self.mcp.calls[0][1]["markdown"]
        self.assertIn("### Added", markdown)
        self.assertNotIn("### Fixed", markdown)

    def test_cleaning_strips_mentions_links_formatting(self):
        self.seed("<@U42> *shipped* the <https://ex.co|billing page> ~today~")
        release_notes.handle("compile release notes", self.ctx())
        markdown = self.mcp.calls[0][1]["markdown"]
        self.assertIn("- Shipped the billing page today", markdown)
        for junk in ("<@", "*", "~", "https://"):
            self.assertNotIn(junk, markdown)

    def test_nothing_shippable_does_not_publish(self):
        self.seed("anyone up for lunch?")
        blocks, text = release_notes.handle("compile the release notes", self.ctx())
        self.assertEqual(self.mcp.calls, [])
        self.assertIn("Nothing shippable found in the last 7 days", text)
        self.assertIn("Nothing shippable", str(blocks))

    def test_publish_called_with_markdown_and_version(self):
        self.seed("released the mobile app v2")
        release_notes.handle("compile the release", self.ctx())
        self.assertEqual(len(self.mcp.calls), 1)
        name, arguments = self.mcp.calls[0]
        self.assertEqual(name, "publish_changelog")
        self.assertTrue(arguments["version"].startswith("Week of "))
        self.assertIn("### Added", arguments["markdown"])
        self.assertIn("- Released the mobile app v2", arguments["markdown"])

    def test_card_shows_publish_path(self):
        stub = StubMcp(result={"path": "/repo/CHANGELOG.md"})
        self.seed("hotfix deployed, resolved the outage")
        blocks, _ = release_notes.handle("compile the release notes", self.ctx(mcp=stub))
        flat = str(blocks)
        self.assertIn("/repo/CHANGELOG.md", flat)
        self.assertIn("publish_changelog", flat)
        self.assertEqual(blocks[0]["type"], "header")

    def test_last_week_widens_window(self):
        self.seed("shipped dark mode", days_ago=10)
        release_notes.handle("compile the release notes", self.ctx())
        self.assertEqual(self.mcp.calls, [])
        release_notes.handle("compile release notes for last week", self.ctx())
        self.assertEqual(len(self.mcp.calls), 1)
        self.assertIn("dark mode", self.mcp.calls[0][1]["markdown"])

    def test_mcp_failure_returns_card_not_exception(self):
        stub = StubMcp(error=RuntimeError("mcp down"))
        self.seed("shipped the importer")
        blocks, text = release_notes.handle("changelog", self.ctx(mcp=stub))
        self.assertTrue(blocks)
        self.assertIn("could not publish", str(blocks).lower())
        self.assertIn("Release notes", text)

    def test_llm_line_mismatch_falls_back_to_cleaned(self):
        self.seed("shipped the exporter")
        with patch("cadence.llm.draft", return_value="One\nTwo\nThree"):
            release_notes.handle("compile the release notes", self.ctx())
        self.assertIn("- Shipped the exporter", self.mcp.calls[0][1]["markdown"])

    def test_llm_polish_applied_when_valid(self):
        self.seed("shipped the exporter")
        with patch("cadence.llm.draft", return_value="You can now export your data"):
            release_notes.handle("compile the release notes", self.ctx())
        self.assertIn("- You can now export your data", self.mcp.calls[0][1]["markdown"])


if __name__ == "__main__":
    unittest.main()
