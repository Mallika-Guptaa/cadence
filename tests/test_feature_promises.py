import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from cadence.features import FeatureContext, promises
from cadence.store import Store

MONDAY = datetime(2026, 7, 6, 10, 0, tzinfo=promises.TZ)  # a Monday morning


class StubMcp:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"id": "TASK-42"} if result is None else result
        self.error = error

    def call_tool(self, name, arguments, timeout=5.0):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


def meta_for(ts, user_id="U1", user_name="Priya", channel_id="C1"):
    return {
        "channel_id": channel_id, "channel_name": "#eng", "ts": ts,
        "user_id": user_id, "user_name": user_name, "permalink": None,
    }


class PromisesTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.mcp = StubMcp()
        self.ctx = FeatureContext(store=self.store, mcp=lambda: self.mcp, client=None, now=MONDAY)

    # -- scanning ---------------------------------------------------------------

    def test_commitment_detected_and_stored(self):
        ts = str(MONDAY.timestamp())
        promises.scan_message("I'll send the report tomorrow", meta_for(ts), self.ctx)
        rows = self.store.open_promises()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner_name"], "Priya")
        expected = (MONDAY + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
        self.assertEqual(rows[0]["due_ts"], expected.timestamp())

    def test_question_and_long_message_not_detected(self):
        ts = str(MONDAY.timestamp())
        promises.scan_message("I'll get the review done tomorrow, right?", meta_for(ts), self.ctx)
        promises.scan_message("I'll handle it " + "x" * 300, meta_for(ts, channel_id="C2"), self.ctx)
        self.assertEqual(self.store.open_promises(), [])

    def test_rescan_dedupes(self):
        ts = str(MONDAY.timestamp())
        for _ in range(2):
            promises.scan_message("We'll ship the fix by Friday", meta_for(ts), self.ctx)
        self.assertEqual(len(self.store.open_promises()), 1)

    # -- due parsing --------------------------------------------------------------

    def test_due_weekday_next_occurrence(self):
        due = promises._parse_due("i'll ship it by friday", MONDAY.timestamp())
        friday = (MONDAY + timedelta(days=4)).replace(hour=17, minute=0, second=0, microsecond=0)
        self.assertEqual(due, friday.timestamp())
        # same weekday rolls to next week
        due = promises._parse_due("i'll ship it by monday", MONDAY.timestamp())
        next_monday = (MONDAY + timedelta(days=7)).replace(hour=17, minute=0, second=0, microsecond=0)
        self.assertEqual(due, next_monday.timestamp())

    def test_due_eod_same_day(self):
        due = promises._parse_due("I'll post the numbers by EOD", MONDAY.timestamp())
        self.assertEqual(due, MONDAY.replace(hour=17, minute=0, second=0, microsecond=0).timestamp())

    def test_due_relative_and_none(self):
        week = promises._parse_due("i'll draft it next week", MONDAY.timestamp())
        self.assertEqual(week, (MONDAY + timedelta(days=7)).replace(hour=17, minute=0, second=0, microsecond=0).timestamp())
        sprint = promises._parse_due("we'll land it next sprint", MONDAY.timestamp())
        self.assertEqual(sprint, (MONDAY + timedelta(days=14)).replace(hour=17, minute=0, second=0, microsecond=0).timestamp())
        self.assertIsNone(promises._parse_due("i'll circle back on that", MONDAY.timestamp()))

    # -- digest ---------------------------------------------------------------------

    def test_digest_shows_overdue_first_with_buttons(self):
        self.store.add_promise("U1", "Priya", "fix the flaky test", (MONDAY - timedelta(days=2)).timestamp(), "C1", "1.0", None)
        self.store.add_promise("U2", "Marco", "draft the RFC", (MONDAY + timedelta(days=3)).timestamp(), "C1", "2.0", None)
        blocks, fallback = promises.handle("show open promises", self.ctx)

        self.assertEqual(blocks[0]["type"], "header")
        sections = [b["text"]["text"] for b in blocks if b["type"] == "section"]
        self.assertIn(":red_circle:", sections[0])
        self.assertIn("fix the flaky test", sections[0])
        self.assertIn("draft the RFC", sections[1])
        self.assertNotIn(":red_circle:", sections[1])

        actions = [b for b in blocks if b["type"] == "actions"]
        self.assertEqual(len(actions), 2)
        done, task = actions[0]["elements"]
        self.assertTrue(done["action_id"].startswith("promise_done"))
        self.assertTrue(task["action_id"].startswith("promise_task"))
        self.assertIn("id", json.loads(done["value"]))
        self.assertEqual(json.loads(task["value"])["owner"], "Priya")
        self.assertIn("1 overdue", fallback)

    def test_digest_empty_state(self):
        blocks, fallback = promises.handle("promises", self.ctx)
        self.assertEqual(fallback, "No open promises")
        self.assertIn("No open promises", blocks[1]["text"]["text"])

    # -- actions --------------------------------------------------------------------

    def test_done_action_updates_status(self):
        pid = self.store.add_promise("U1", "Priya", "fix the flaky test", None, "C1", "1.0", None)
        blocks, text = promises.ACTIONS["promise_done"]({"id": pid}, self.ctx)
        self.assertEqual(self.store.get_promise(pid)["status"], "done")
        self.assertIn(":white_check_mark:", blocks[0]["text"]["text"])

    def test_task_action_files_via_mcp_and_guards_failure(self):
        pid = self.store.add_promise("U2", "Marco", "draft the RFC", None, "C1", "2.0", None)
        blocks, text = promises.ACTIONS["promise_task"](
            {"id": pid, "title": "draft the RFC", "owner": "Marco", "due": "2026-07-09"}, self.ctx
        )
        self.assertEqual(self.store.get_promise(pid)["status"], "task_filed")
        self.assertIn("TASK-42", blocks[0]["text"]["text"])
        name, args = self.mcp.calls[0]
        self.assertEqual(name, "create_task")
        self.assertEqual(args["owner"], "Marco")

        broken = FeatureContext(
            store=self.store, mcp=lambda: StubMcp(error=RuntimeError("mcp down")), client=None, now=MONDAY,
        )
        pid2 = self.store.add_promise("U1", "Priya", "fix the flaky test", None, "C1", "3.0", None)
        blocks, text = promises.ACTIONS["promise_task"]({"id": pid2}, broken)
        self.assertIn(":warning:", blocks[0]["text"]["text"])
        self.assertEqual(self.store.get_promise(pid2)["status"], "open")


if __name__ == "__main__":
    unittest.main()


class RankAndDedupeTests(unittest.TestCase):
    """Digest prioritization: overdue -> due-soon -> concrete -> vague; dupes collapsed."""

    def setUp(self):
        import tempfile
        from cadence.store import Store
        self.store = Store(tempfile.mktemp(suffix=".db"))
        self.now = 1_000_000.0

    def _add(self, text, due=None):
        return self.store.add_promise("U", "Priya", text, due, "C1", f"{id(text)}", None)

    def test_priority_order(self):
        from cadence.features.promises import _rank_and_dedupe
        self._add("I can pair on redis tomorrow if that helps")          # vague -> tier 4
        self._add("look into the flaky test")                            # other -> tier 3
        self._add("send the pricing deck")                               # concrete -> tier 2
        self._add("finish the migration doc", due=self.now + 86400)      # due-soon -> tier 1
        self._add("ship the hotfix", due=self.now - 3600)                # overdue -> tier 0
        rows = self.store.open_promises()
        ranked, collapsed = _rank_and_dedupe(rows, self.now)
        order = [r["text"][:12] for r in ranked]
        self.assertEqual(order[0], "ship the hot")   # overdue first
        self.assertEqual(order[1], "finish the m")   # then due-soon
        self.assertEqual(order[2], "send the pri")   # then concrete
        self.assertEqual(order[-1], "I can pair o")  # vague offer last
        self.assertEqual(collapsed, 0)

    def test_dedupe_collapses_identical_text(self):
        from cadence.features.promises import _rank_and_dedupe
        for who in ("Tom", "Sam", "Ravi"):
            self.store.add_promise(who, who, "I can pair on kubernetes ingress tomorrow if that helps",
                                   None, "C1", f"ts_{who}", None)
        self._add("send the Q3 numbers", due=self.now + 3600)
        rows = self.store.open_promises()
        ranked, collapsed = _rank_and_dedupe(rows, self.now)
        pair = [r for r in ranked if "pair on kubernetes" in r["text"]]
        self.assertEqual(len(pair), 1)      # 3 identical -> 1 kept
        self.assertEqual(collapsed, 2)      # 2 collapsed
        self.assertEqual(ranked[0]["text"][:4], "send")  # concrete+due beats the offer
