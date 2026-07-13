"""Tests for the Deja Vu duplicate-discussion feature. No network, no LLM."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cadence.features import FeatureContext
from cadence.features import dejavu
from cadence.store import Store

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 7, 11, 12, 0, tzinfo=TZ)


def slack_ts(dt: datetime) -> str:
    return f"{dt.timestamp():.6f}"


class StubMcp:
    def call_tool(self, name, args):
        raise AssertionError("MCP must not be called by deja vu")


class FakeClient:
    def __init__(self):
        self.posts = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True}

    def chat_getPermalink(self, **kwargs):
        return {"permalink": "https://slack.example/p123"}


class DejaVuTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "test.db")

    def ctx(self, client=None, extras=None) -> FeatureContext:
        return FeatureContext(
            store=self.store, mcp=lambda: StubMcp(), client=client,
            user_id="U1", user_name="alex", now=NOW, extras=extras or {},
        )

    def seed(self, text: str, when: datetime, channel=("C1", "#eng"), user=("U2", "priya")):
        self.store.upsert_message(channel[0], channel[1], slack_ts(when), user[0], user[1], text)

    def test_prior_discussion_found(self):
        self.seed("we should move the retro cadence to biweekly", NOW - timedelta(days=30))
        blocks, fallback = dejavu.handle("was this discussed before: retro cadence", self.ctx())
        flat = json.dumps(blocks)
        self.assertIn("This came up before", flat)
        self.assertIn("#eng", flat)
        self.assertIn("Jun", flat)
        self.assertIn("2026", flat)
        self.assertIn("prior discussion", fallback)

    def test_recent_only_matches_are_new_ground(self):
        self.seed("we should move the retro cadence to biweekly", NOW - timedelta(hours=1))
        blocks, fallback = dejavu.handle("was this discussed before: retro cadence", self.ctx())
        flat = json.dumps(blocks)
        self.assertIn("new ground", flat)
        self.assertIn(":seedling:", flat)
        self.assertIn("No prior discussion", fallback)

    def test_two_term_threshold_enforced(self):
        self.seed("our cadence is fine as is", NOW - timedelta(days=30))
        blocks, _ = dejavu.handle("did we already discuss the retro cadence", self.ctx())
        self.assertIn("new ground", json.dumps(blocks))

    def test_no_topic_prompts_for_one(self):
        blocks, fallback = dejavu.handle("deja vu", self.ctx())
        self.assertIn("topic", json.dumps(blocks).lower())
        self.assertIn("topic", fallback.lower())

    def test_handle_never_raises(self):
        broken = FeatureContext(store=None, mcp=lambda: StubMcp(), now=NOW)
        blocks, fallback = dejavu.handle("was this discussed before: retro cadence", broken)
        self.assertIn(":warning:", json.dumps(blocks))
        self.assertTrue(fallback)

    def test_passive_silent_without_live_scan(self):
        self.seed("postgres connection pool exhaustion during deploy spikes caused the outage", NOW - timedelta(days=3))
        client = FakeClient()
        text = "we are seeing postgres connection pool exhaustion again during the deploy today, anyone have context on this?"
        meta = {"channel_id": "C2", "channel_name": "#ops", "ts": slack_ts(NOW), "user_id": "U3", "user_name": "marco"}
        dejavu.scan_message(text, meta, self.ctx(client=client, extras={}))
        self.assertEqual(client.posts, [])

    def test_passive_silent_without_client(self):
        self.seed("postgres connection pool exhaustion during deploy spikes caused the outage", NOW - timedelta(days=3))
        text = "we are seeing postgres connection pool exhaustion again during the deploy today, anyone have context on this?"
        meta = {"channel_id": "C2", "channel_name": "#ops", "ts": slack_ts(NOW), "user_id": "U3", "user_name": "marco"}
        dejavu.scan_message(text, meta, self.ctx(client=None, extras={"live_scan": True}))  # must not raise

    def test_passive_short_text_ignored(self):
        self.seed("postgres connection pool exhaustion during deploy spikes caused the outage", NOW - timedelta(days=3))
        client = FakeClient()
        meta = {"channel_id": "C2", "channel_name": "#ops", "ts": slack_ts(NOW), "user_id": "U3", "user_name": "marco"}
        dejavu.scan_message("postgres connection pool exhaustion again", meta, self.ctx(client=client, extras={"live_scan": True}))
        self.assertEqual(client.posts, [])

    def test_passive_posts_once_on_strong_match(self):
        self.seed("postgres connection pool exhaustion during deploy spikes caused the outage last week", NOW - timedelta(days=3))
        client = FakeClient()
        ctx = self.ctx(client=client, extras={"live_scan": True})
        text = "we are seeing postgres connection pool exhaustion again during the deploy today, anyone have context on this?"
        meta = {"channel_id": "C2", "channel_name": "#ops", "ts": slack_ts(NOW), "user_id": "U3", "user_name": "marco"}
        dejavu.scan_message(text, meta, ctx)
        self.assertEqual(len(client.posts), 1)
        post = client.posts[0]
        self.assertEqual(post["channel"], "C2")
        self.assertEqual(post["thread_ts"], meta["ts"])
        self.assertIn("https://slack.example/p123", post["text"])
        dejavu.scan_message(text, meta, ctx)  # same message again -> still one post
        self.assertEqual(len(client.posts), 1)

    def test_passive_weak_match_stays_quiet(self):
        self.seed("the postgres migration guide is ready for review", NOW - timedelta(days=3))
        client = FakeClient()
        text = "having trouble with postgres migration errors on the staging cluster today, logs attached for the curious"
        meta = {"channel_id": "C2", "channel_name": "#ops", "ts": slack_ts(NOW), "user_id": "U3", "user_name": "marco"}
        dejavu.scan_message(text, meta, self.ctx(client=client, extras={"live_scan": True}))
        self.assertEqual(client.posts, [])


if __name__ == "__main__":
    unittest.main()


class SummaryTests(unittest.TestCase):
    def test_deterministic_summary_prefers_decision_message(self):
        from cadence.features.dejavu import _summarize

        class R(dict):
            def __getitem__(self, k): return dict.get(self, k)
        prior = [
            R(ts_num=1.0, user_name="Priya", text="we should look into SSO options"),
            R(ts_num=2.0, user_name="Marco", text="we decided to require SAML for tier-1 accounts"),
        ]
        with patch("cadence.llm.draft", return_value=None):
            s = _summarize(prior, "sso")
        self.assertIn("SAML for tier-1", s)  # decision message wins the fallback

    def test_llm_summary_used_when_available(self):
        from cadence.features.dejavu import _summarize

        class R(dict):
            def __getitem__(self, k): return dict.get(self, k)
        prior = [R(ts_num=1.0, user_name="Priya", text="sso discussion")]
        with patch("cadence.llm.draft", return_value="Team agreed to require SAML for enterprise."):
            s = _summarize(prior, "sso")
        self.assertEqual(s, "Team agreed to require SAML for enterprise.")
