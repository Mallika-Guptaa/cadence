"""Catch-Me-Up — personalized digest of what happened while the user was away.

Two things people actually want after time off: (1) where they were pulled in
by name/@-mention, and (2) the few things that actually happened in each busy
channel — not a raw message count. So the digest shows a "mentioned you"
section plus, per channel, up to three representative highlights (deduped and
ranked by signal), each linked. "details" widens the channel count and the
mention list; the base view stays one screen.

Deterministic core (window parsing, highlight selection) is unit-tested; an
optional one-line LLM TL;DR sits on top with a graceful skip when absent.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import cadence.features as features
from .. import llm, style

KIND = "catch_me_up"
KEYWORDS = ("what did i miss", "catch me up", "i was out", "while i was away", "away since", "what happened while")

TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# messages worth surfacing carry decision / shipping / risk / ownership signal
_SIGNAL_RE = re.compile(
    r"\b(decid|decision|agree|approv|final call|ship|shipped|launch|releas|deploy|"
    r"fix|fixed|bug|broke|blocked|blocker|outage|incident|regress|root cause|"
    r"deprecat|migrat|owe|i'?ll|we'?ll|by (?:mon|tue|wed|thu|fri|today|tomorrow|eod)|\?)",
    re.I,
)

CONCISE_CHANNELS = 3
DETAIL_CHANNELS = 8
HIGHLIGHTS_PER_CHANNEL = 3
PERMALINK_CAP = 16


def _parse_window(text: str, now: datetime) -> datetime:
    """Start of the absence window parsed from the request text."""
    lowered = text.lower()
    match = re.search(r"since\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)", lowered)
    if match:
        days_back = (now.weekday() - _WEEKDAYS[match.group(1)]) % 7 or 7
        return (now - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in lowered:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    match = re.search(r"(\d+)\s*days?", lowered)
    if match:
        return now - timedelta(days=int(match.group(1)))
    return now - timedelta(days=2)


def _channel_label(row) -> str:
    # passively-cached messages may lack a channel name; <#id> renders in Slack
    return row["channel_name"] or f"<#{row['channel_id']}>"


def _mentions(rows: list, ctx: features.FeatureContext) -> list:
    """Rows that @-mention the user or name them (excluding the 'you' placeholder)."""
    mention = f"<@{ctx.user_id}>" if ctx.user_id else None
    name = (ctx.user_name or "").strip()
    name_re = re.compile(rf"\b{re.escape(name)}\b", re.I) if name and name.lower() != "you" else None
    hits = []
    for row in rows:
        text = row["text"] or ""
        if (mention and mention in text) or (name_re and name_re.search(text)):
            hits.append(row)
    return hits


def _dedupe_key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()[:70]


def _highlights(rows: list, k: int = HIGHLIGHTS_PER_CHANNEL) -> list:
    """Up to k representative, distinct messages from one channel (chronological)."""
    seen: set[str] = set()
    scored: list[tuple[float, Any]] = []
    for row in rows:
        text = (row["text"] or "").strip()
        if len(text) < 15:
            continue
        key = _dedupe_key(text)
        if key in seen:
            continue
        seen.add(key)
        score = 2.0 * len(_SIGNAL_RE.findall(text)) + min(len(text) / 90.0, 2.0)
        scored.append((score, row))
    scored.sort(key=lambda s: s[0], reverse=True)
    picked = [row for _, row in scored[:k]]
    picked.sort(key=lambda r: float(r["ts"]))  # read top-to-bottom in time order
    return picked


class _Permalinks:
    """Fetch permalinks lazily under a global budget (rate-limit friendly)."""

    def __init__(self, ctx: features.FeatureContext, cap: int = PERMALINK_CAP):
        self.ctx, self.cap, self.used = ctx, cap, 0

    def get(self, row) -> str | None:
        if self.used >= self.cap:
            return None
        url = row["permalink"] or features.permalink(self.ctx.client, row["channel_id"], row["ts"])
        if url:
            self.used += 1
        return url


def _mention_section(hits: list, links: _Permalinks, budget: int) -> dict:
    lines = []
    for row in hits[:budget]:
        who = row["user_name"] or "someone"
        snippet = style.truncate(row["text"] or "", 110)
        lines.append(f"*{_channel_label(row)}* — {who}: {style.link(links.get(row), snippet)}")
    body = "\n".join(f"• {ln}" for ln in lines)
    if len(hits) > budget:
        body += f"\n_…and {len(hits) - budget} more mention(s) — ask for details_"
    return style.section(f"*💬 You were mentioned ({len(hits)})*\n{body}")


def _channel_section(channel: str, rows: list, links: _Permalinks) -> dict:
    lines = [f"*{channel}*  ·  {len(rows)} messages"]
    for row in _highlights(rows):
        who = row["user_name"] or "someone"
        snippet = style.truncate(row["text"] or "", 130)
        lines.append(f"• {who}: {style.link(links.get(row), snippet)}")
    return style.section("\n".join(lines))


def _tldr(mentions: list, top_rows: list) -> str | None:
    sample = "\n".join(
        f"[{_channel_label(r)}] {r['user_name'] or 'someone'}: {style.truncate(r['text'] or '', 160)}"
        for r in (mentions[:4] + top_rows[:10])
    )
    if not sample:
        return None
    drafted = llm.draft(
        "In ONE sentence, tell a teammate returning from time off the single most "
        "important thing from these Slack messages. No preamble." + style.CONCISE_LLM_SUFFIX,
        sample,
    )
    if not drafted:
        return None
    line = drafted.strip().splitlines()[0].lstrip("•-* ").strip()
    return line or None


def handle(text: str, ctx: features.FeatureContext) -> tuple[list[dict[str, Any]], str]:
    try:
        now = ctx.now or datetime.now(TZ)
        since = _parse_window(text, now)
        since_label = since.strftime("%a %b %-d")
        detail = style.wants_detail(text)

        features.sync_channels(ctx)
        rows = ctx.store.messages_since(since.timestamp(), limit=600)
        if not rows:
            message = f"Nothing new since {since_label}."
            return [style.header("While you were out"), style.section(message)], message

        mentions = _mentions(rows, ctx)
        links = _Permalinks(ctx)

        by_channel: dict[str, list] = {}
        for row in rows:
            by_channel.setdefault(_channel_label(row), []).append(row)
        ranked = sorted(by_channel.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        max_channels = DETAIL_CHANNELS if detail else CONCISE_CHANNELS
        shown, remaining = ranked[:max_channels], ranked[max_channels:]

        blocks: list[dict[str, Any]] = [style.header("While you were out")]
        tldr = _tldr(mentions, rows)
        if tldr:
            blocks.append(style.section(f"*TL;DR* — {tldr}"))

        blocks.append(
            _mention_section(mentions, links, budget=10 if detail else 3)
            if mentions
            else style.section("*💬 You were mentioned (0)*\nNobody tagged you directly — nothing needs an immediate reply. :ok_hand:")
        )

        for channel, ch_rows in shown:
            blocks.append(_channel_section(channel, ch_rows, links))
        if remaining:
            extra = ", ".join(name for name, _ in remaining[:6])
            more = f"_…and {len(remaining)} more channel(s): {extra}"
            more += " — ask 'catch me up with details'_" if not detail else "_"
            blocks.append(style.section(more))

        blocks.append(style.context(
            f"Since {since_label} — {len(rows)} messages across {len(by_channel)} channels"
            + ("" if detail else " · say \"with details\" for more")
        ))

        fallback = (
            f"While you were out: {len(rows)} messages across {len(by_channel)} channels since "
            f"{since_label}; {len(mentions)} mention you."
        )
        return blocks, fallback
    except Exception as exc:  # noqa: BLE001 - never raise out of handle()
        message = f":warning: Couldn't build your catch-up digest ({exc})."
        return [style.section(message)], "Couldn't build your catch-up digest."
