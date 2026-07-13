"""Proactive morning digest: Cadence posts the team's rhythm without being asked.

Every morning (CADENCE_DIGEST_HOUR, local TZ) a digest lands in
CADENCE_DIGEST_CHANNEL: your meetings today, open promises (overdue first),
and yesterday's channel activity. CADENCE_DIGEST_ON_START=1 posts one at
startup — handy for demos.

Content building is pure (unit-tested); only the scheduler thread and the
Slack post are side effects.
"""

from __future__ import annotations

import os
import threading
import time as time_mod
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import style
from .features import FeatureContext, log, permalink, sync_channels

TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))


def seconds_until_next(hour: int, now: datetime) -> float:
    """Seconds from `now` to the next occurrence of hour:00 local."""
    target = datetime.combine(now.date(), time(hour, 0), tzinfo=now.tzinfo)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def build_digest_blocks(ctx: FeatureContext, me_email: str) -> tuple[list[dict], str]:
    now = ctx.now or datetime.now(TZ)
    blocks: list[dict] = [style.header(f"🎼 Daily Cadence — {now.strftime('%a %b %-d')}")]

    # today's meetings (via MCP calendar)
    meetings: list[str] = []
    try:
        day_start = datetime.combine(now.date(), time(0, 0), tzinfo=now.tzinfo)
        day_end = datetime.combine(now.date(), time(23, 59), tzinfo=now.tzinfo)
        events = ctx.mcp().call_tool(
            "get_events",
            {"email": me_email, "start": day_start.isoformat(), "end": day_end.isoformat()},
        )
        for e in sorted(events, key=lambda e: e["start"]):
            if e.get("kind") != "meeting":
                continue
            at = datetime.fromisoformat(e["start"]).strftime("%-H:%M")
            meetings.append(f"{at} — {e['title']}")
    except Exception as exc:  # noqa: BLE001 - digest must post even if MCP is down
        log(f"digest: calendar unavailable: {exc}")
    if meetings:
        blocks.append(style.section("*Today's meetings*\n" + style.bullets(meetings, 5)))
    else:
        blocks.append(style.section("*Today's meetings*\nNo meetings on your calendar. :tada:"))

    # open promises — prioritized + deduped (shared with the on-demand digest)
    rows = ctx.store.open_promises()
    if rows:
        from .features.promises import _rank_and_dedupe

        now_ts = now.timestamp()
        overdue_ids = {r["id"] for r in rows if r["due_ts"] is not None and float(r["due_ts"]) < now_ts}
        ranked, _ = _rank_and_dedupe(rows, now_ts)
        items = []
        for r in ranked[:5]:
            owner = r["owner_name"] or "someone"
            prefix = ":red_circle: " if r["id"] in overdue_ids else ""
            url = r["permalink"] or permalink(ctx.client, r["channel_id"], r["message_ts"])
            items.append(f"{prefix}*{owner}* — {style.link(url, style.truncate(r['text'], 90))}")
        blocks.append(style.section(f"*Open promises ({len(rows)}, {len(overdue_ids)} overdue)*\n" + "\n".join(items)))

    # yesterday's activity
    rows = ctx.store.messages_since(now.timestamp() - 86400, limit=400)
    if rows:
        by_channel: dict[str, int] = {}
        for r in rows:
            name = r["channel_name"] or f"<#{r['channel_id']}>"
            by_channel[name] = by_channel.get(name, 0) + 1
        busiest = sorted(by_channel.items(), key=lambda kv: -kv[1])[:4]
        line = " · ".join(f"{name} ({count})" for name, count in busiest)
        blocks.append(style.section(f"*Last 24h*\n{len(rows)} messages — busiest: {line}"))

    blocks.append(style.context("Posted automatically by Cadence · ask me `show open promises` or `catch me up` for details"))
    return blocks, "Daily Cadence digest"


def start_scheduler(client, ctx_factory, me_email: str) -> None:
    """Post the digest every morning; optionally once at startup (demo)."""
    channel_name = os.environ.get("CADENCE_DIGEST_CHANNEL", "general")
    if not channel_name:
        log("digest: disabled (CADENCE_DIGEST_CHANNEL empty)")
        return
    hour = int(os.environ.get("CADENCE_DIGEST_HOUR", "9"))

    def resolve_channel() -> str | None:
        if channel_name.startswith(("C", "G")) and channel_name.isalnum() and channel_name.isupper():
            return channel_name
        try:
            for c in client.conversations_list(types="public_channel", limit=200).get("channels", []):
                if c["name"] == channel_name.lstrip("#"):
                    return c["id"]
        except Exception as exc:  # noqa: BLE001
            log(f"digest: channel lookup failed: {exc}")
        return None

    def post_digest() -> None:
        channel_id = resolve_channel()
        if not channel_id:
            log(f"digest: channel '{channel_name}' not found, skipping")
            return
        ctx = ctx_factory()
        try:
            sync_channels(ctx)
        except Exception as exc:  # noqa: BLE001
            log(f"digest: sync failed, posting from cache: {exc}")
        blocks, text = build_digest_blocks(ctx, me_email)
        try:
            client.chat_postMessage(channel=channel_id, blocks=blocks, text=text)
            log(f"digest: posted to #{channel_name}")
        except Exception as exc:  # noqa: BLE001
            log(f"digest: post failed: {exc}")

    def loop() -> None:
        if os.environ.get("CADENCE_DIGEST_ON_START", "") == "1":
            post_digest()
        while True:
            wait = seconds_until_next(hour, datetime.now(TZ))
            log(f"digest: next post in {wait / 3600:.1f}h (daily at {hour:02d}:00 {TZ.key}, #{channel_name})")
            time_mod.sleep(wait)
            post_digest()

    threading.Thread(target=loop, name="cadence-digest", daemon=True).start()
