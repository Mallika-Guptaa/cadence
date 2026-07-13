"""Feature registry — Cadence's productivity skills plug in here.

Contract for a feature module (see promises.py for the reference example):

    KIND: str                     # unique intent kind, e.g. "promises"
    KEYWORDS: tuple[str, ...]     # lowercase routing triggers
    def handle(text: str, ctx: FeatureContext) -> tuple[list[dict], str]
        # returns (blocks, fallback_text). Must be deterministic-safe:
        # LLM calls only through cadence.llm with a non-LLM fallback.
    def scan_message(text: str, meta: dict, ctx: FeatureContext) -> None
        # OPTIONAL passive hook, called for every cached channel message.
        # meta: channel_id, channel_name, ts, user_id, user_name, permalink
    ACTIONS: dict[str, callable]  # OPTIONAL: action_id prefix -> handler
        # handler(payload: dict, ctx: FeatureContext) -> tuple[blocks, text]

Rules for features: use style.py helpers for all cards (concise by default,
honor style.wants_detail(text)); never raise out of handle() — return an
error card; do not import slack_app (ctx carries everything you need).
"""

from __future__ import annotations

import importlib
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

FEATURE_MODULES = ["promises", "catchup", "status", "experts", "release_notes", "dejavu"]


@dataclass
class FeatureContext:
    store: Any                      # cadence.store.Store
    mcp: Callable[[], Any]          # lazy McpCalendarClient getter
    client: Any = None              # slack_sdk WebClient (None in unit tests)
    user_id: str | None = None
    user_name: str = "you"
    channel_id: str | None = None
    team_id: str | None = None
    now: datetime | None = None
    extras: dict = field(default_factory=dict)


def log(message: str) -> None:
    print(f"[cadence] {message}", file=sys.stderr, flush=True)


_loaded: dict[str, Any] | None = None


def all_features() -> dict[str, Any]:
    global _loaded
    if _loaded is None:
        _loaded = {}
        for name in FEATURE_MODULES:
            try:
                module = importlib.import_module(f"cadence.features.{name}")
                _loaded[module.KIND] = module
            except Exception as exc:  # noqa: BLE001 - a broken feature must not sink the app
                log(f"feature '{name}' failed to load: {exc}")
    return _loaded


def route(text: str) -> Any | None:
    """Keyword router: first feature whose KEYWORDS appear in the text."""
    lowered = text.lower()
    for module in all_features().values():
        if any(k in lowered for k in module.KEYWORDS):
            return module
    return None


def by_kind(kind: str) -> Any | None:
    return all_features().get(kind)


def action_handlers() -> dict[str, Callable]:
    handlers: dict[str, Callable] = {}
    for module in all_features().values():
        handlers.update(getattr(module, "ACTIONS", {}))
    return handlers


def scan_hooks() -> list[Callable]:
    return [
        module.scan_message
        for module in all_features().values()
        if hasattr(module, "scan_message")
    ]


def parallel_map(fn: Callable, items: Iterable, max_workers: int = 8) -> list:
    """Fan work out across threads (channel syncs, per-item lookups)."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        return list(pool.map(fn, items))


# -- shared Slack helpers ------------------------------------------------------

_CARD_BLOCK_TYPES = {"section", "header", "actions", "divider", "image"}


def _is_card(message: dict) -> bool:
    """True for Block Kit cards (agent output); plain text has only rich_text blocks."""
    return any(b.get("type") in _CARD_BLOCK_TYPES for b in message.get("blocks") or [])


def permalink(client: Any, channel_id: str, ts: str) -> str | None:
    if client is None:
        return None
    try:
        return client.chat_getPermalink(channel=channel_id, message_ts=ts).get("permalink")
    except Exception:  # noqa: BLE001 - permalinks are nice-to-have
        return None


MAX_SYNC_CHANNELS = 20  # rate-limit budget: history is Tier-3 (~50/min)
_names_cache: dict[str, str] = {}  # user_id -> display name, shared across syncs


def sync_channels(ctx: FeatureContext, per_channel: int = 200) -> int:
    """Pull recent history for every public channel the bot is in into the store.

    Incremental: uses sync_state to fetch only messages newer than the last
    sync, so repeated calls are cheap even on big workspaces. Channel count is
    capped per sync to stay inside Slack's history rate limits.
    """
    if ctx.client is None:
        return 0
    try:
        payload: dict[str, Any] = {"types": "public_channel", "exclude_archived": True, "limit": 200}
        if ctx.team_id:
            payload["team_id"] = ctx.team_id
        channels = [
            c for c in ctx.client.conversations_list(**payload).get("channels", [])
            if c.get("is_member")
        ]
        if len(channels) > MAX_SYNC_CHANNELS:
            log(f"sync: capping at {MAX_SYNC_CHANNELS} of {len(channels)} member channels (rate-limit budget)")
            channels = channels[:MAX_SYNC_CHANNELS]
    except Exception as exc:  # noqa: BLE001
        log(f"sync: conversations.list failed: {exc}")
        return 0

    def name_of(user_id: str | None) -> str | None:
        if not user_id:
            return None
        if user_id not in _names_cache:
            try:
                info = ctx.client.users_info(user=user_id)["user"]
                _names_cache[user_id] = info.get("real_name") or info.get("name") or user_id
            except Exception:  # noqa: BLE001
                _names_cache[user_id] = user_id
        return _names_cache[user_id]

    bot_mention = f"<@{ctx.extras['bot_user_id']}>" if ctx.extras.get("bot_user_id") else None

    def index_message(message: dict, channel_id: str, channel_name: str) -> bool:
        subtype = message.get("subtype")
        if subtype and subtype != "bot_message":
            return False  # joins, edits, etc. — but keep bot_message (persona posts)
        if (message.get("bot_id") or subtype == "bot_message") and _is_card(message):
            return False  # skip Cadence's own cards; keep plain-text bot posts (personas, CI)
        text = message.get("text") or ""
        if bot_mention and bot_mention in text:
            return False  # requests aimed at Cadence are not workspace intelligence
        ts = message["ts"]
        # custom-username bot posts (multi-persona seeding) carry no user id
        uid = message.get("user") or (f"persona:{message['username']}" if message.get("username") else None)
        uname = name_of(message.get("user")) or message.get("username")
        ctx.store.upsert_message(channel_id, f"#{channel_name}", ts, uid, uname, text)
        meta = {
            "channel_id": channel_id, "channel_name": f"#{channel_name}", "ts": ts,
            "user_id": uid, "user_name": uname, "permalink": None,
        }
        for hook in scan_hooks():
            try:
                hook(text, meta, ctx)
            except Exception as exc:  # noqa: BLE001
                log(f"scan hook failed: {exc}")
        return True

    total = 0
    for channel in channels:
        channel_id, channel_name = channel["id"], channel.get("name", channel["id"])
        oldest = ctx.store.last_synced_ts(channel_id)
        try:
            kwargs: dict[str, Any] = {"channel": channel_id, "limit": per_channel}
            if oldest:
                kwargs["oldest"] = oldest
            history = ctx.client.conversations_history(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log(f"sync: history failed for #{channel_name}: {exc}")
            continue
        newest = oldest
        for message in history.get("messages", []):
            if index_message(message, channel_id, channel_name):
                total += 1
            # threads hold most of the conversation — pull replies for new parents
            if message.get("reply_count"):
                try:
                    replies = ctx.client.conversations_replies(
                        channel=channel_id, ts=message["ts"], limit=50
                    ).get("messages", [])[1:]
                    for reply in replies:
                        if index_message(reply, channel_id, channel_name):
                            total += 1
                except Exception as exc:  # noqa: BLE001
                    log(f"sync: replies failed in #{channel_name}: {exc}")
            ts = message["ts"]
            if newest is None or float(ts) > float(newest):
                newest = ts
        if newest and newest != oldest:
            ctx.store.mark_synced(channel_id, newest)
    log(f"sync: cached {total} new messages across {len(channels)} channels")
    return total
