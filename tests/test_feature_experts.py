import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from cadence.features import FeatureContext
from cadence.features.experts import KEYWORDS, KIND, _extract_topic, _terms, handle
from cadence.store import Store

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=TZ)


class StubMcp:
    def call_tool(self, name: str, args: dict):
        raise AssertionError("experts feature must not call MCP tools")


def make_ctx(store: Store, user_id: str = "U_ASKER") -> FeatureContext:
    return FeatureContext(
        store=store, mcp=lambda: StubMcp(), client=None,
        user_id=user_id, user_name="Hema", now=NOW,
    )


def all_text(blocks: list[dict]) -> str:
    return json.dumps(blocks)


class TopicExtractionTests(unittest.TestCase):
    def test_topic_extraction(self):
        self.assertEqual(_extract_topic("who knows about kubernetes ingress?"), "kubernetes ingress")
        self.assertEqual(_extract_topic("who can help with payments migration"), "payments migration")
        self.assertEqual(_extract_topic("who understands react hooks"), "react hooks")
        self.assertEqual(_extract_topic("any expert on terraform state?"), "terraform state")

    def test_terms_drop_stopwords_and_short_words(self):
        self.assertEqual(_terms("the payments migration to v2"), ["payments", "migration"])


class HandleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = Store(Path(self._tmp.name) / "test.db")

    def seed(self, user_id: str, user_name: str, texts: list[str], base_ts: float = 1751000000.0):
        for i, text in enumerate(texts):
            self.store.upsert_message("C1", "#eng", f"{base_ts + i:.6f}", user_id, user_name, text)

    def test_ranking_respects_hits(self):
        self.seed("U_ALICE", "Alice", [
            "kubernetes ingress rollout is done",
            "debugging the kubernetes ingress controller",
            "kubernetes upgrade next week",
        ])
        self.seed("U_BOB", "Bob", ["I touched kubernetes once"], base_ts=1751100000.0)
        with patch("cadence.features.experts.draft", return_value=None):
            blocks, fallback = handle("who knows about kubernetes ingress?", make_ctx(self.store))
        text = all_text(blocks)
        self.assertEqual(blocks[0]["type"], "header")
        self.assertIn("kubernetes ingress", blocks[0]["text"]["text"])
        self.assertIn("Alice", text)
        self.assertIn("3 messages", text)
        self.assertLess(text.index("Alice"), text.index("Bob"))
        self.assertIn("view a sample", text)
        self.assertIn("Alice", fallback)

    def test_asker_excluded(self):
        self.seed("U_ALICE", "Alice", ["kubernetes all day", "kubernetes all night"])
        self.seed("U_BOB", "Bob", ["kubernetes sometimes"], base_ts=1751100000.0)
        with patch("cadence.features.experts.draft", return_value=None):
            blocks, _ = handle("who knows about kubernetes?", make_ctx(self.store, user_id="U_ALICE"))
        text = all_text(blocks)
        self.assertNotIn("Alice", text)
        self.assertIn("Bob", text)

    def test_empty_state(self):
        self.seed("U_ALICE", "Alice", ["lunch plans anyone"])
        with patch("cadence.features.experts.draft", return_value=None):
            blocks, fallback = handle("who can help with payments migration", make_ctx(self.store))
        text = all_text(blocks)
        self.assertIn("No one has discussed", text)
        self.assertIn("payments migration", text)
        self.assertIn("#general", text)
        self.assertIn("payments migration", fallback)

    def test_intro_draft_contains_topic_and_top_name(self):
        self.seed("U_ALICE", "Alice", ["payments migration kickoff", "payments migration schema review"])
        with patch("cadence.features.experts.draft", return_value=None):
            blocks, _ = handle("who can help with payments migration", make_ctx(self.store))
        intro_blocks = [b for b in blocks if b["type"] == "section" and "Ready-to-send intro" in b["text"]["text"]]
        self.assertEqual(len(intro_blocks), 1)
        intro = intro_blocks[0]["text"]["text"]
        self.assertIn("Hi Alice", intro)
        self.assertIn("payments migration", intro)
        self.assertIn("Hema", intro)

    def test_llm_polish_used_when_available(self):
        self.seed("U_ALICE", "Alice", ["kubernetes ingress deep dive"])
        with patch("cadence.features.experts.draft", return_value="Polished intro text."):
            blocks, _ = handle("who knows about kubernetes ingress", make_ctx(self.store))
        self.assertIn("Polished intro text.", all_text(blocks))

    def test_no_topic_asks_for_one(self):
        with patch("cadence.features.experts.draft", return_value=None):
            blocks, fallback = handle("who can help", make_ctx(self.store))
        self.assertIn(":warning:", blocks[0]["text"]["text"])
        self.assertIn("topic", fallback.lower())

    def test_handle_never_raises(self):
        class BrokenStore:
            def expertise_for(self, terms, limit=3):
                raise RuntimeError("boom")

        ctx = make_ctx(self.store)
        ctx.store = BrokenStore()
        blocks, fallback = handle("who knows about kubernetes", ctx)
        self.assertIn(":warning:", blocks[0]["text"]["text"])
        self.assertIn(":warning:", fallback)

    def test_contract_surface(self):
        self.assertEqual(KIND, "who_knows")
        self.assertEqual(
            KEYWORDS,
            ("who knows", "who can help", "who understands", "expert on", "expert in"),
        )
        import cadence.features.experts as experts
        self.assertFalse(hasattr(experts, "scan_message"))
        self.assertFalse(hasattr(experts, "ACTIONS"))


if __name__ == "__main__":
    unittest.main()
