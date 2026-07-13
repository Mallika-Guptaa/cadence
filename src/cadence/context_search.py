"""Workspace context retrieval for agendas and handover briefs.

Three tiers, most authoritative first:
1. Real-Time Search API (assistant.search.context) with the bot token — only
   possible when Slack supplies an action_token on the triggering event.
2. RTS with a user token (SLACK_USER_TOKEN) — user-token calls need no
   action_token, so this fires reliably once a token with search:read.* exists.
3. conversations.history keyword scan over channels the bot is in — keeps a
   fresh sandbox demo grounded even with zero RTS entitlements.

Every path records counts in a RetrievalTrace so the Block Kit footer can say
honestly which technology produced the context.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextItem:
    title: str
    content: str
    permalink: str | None = None
    source: str = "history"  # "rts" | "history"


@dataclass
class RetrievalTrace:
    rts_bot: int = 0
    rts_user: int = 0
    history: int = 0
    channels_scanned: list[str] = field(default_factory=list)

    def footer(self) -> str:
        parts = []
        if self.rts_bot or self.rts_user:
            parts.append(f"Real-Time Search API ({self.rts_bot + self.rts_user} results)")
        if self.history:
            parts.append(f"channel history ({self.history})")
        if not parts:
            return "Context: none found"
        footer = "Context: " + " + ".join(parts)
        if self.rts_bot or self.rts_user:  # name the API only when it actually ran
            footer += " — assistant.search.context"
        return footer


def log(message: str) -> None:
    print(f"[cadence] {message}", file=sys.stderr, flush=True)


def search_context(
    client: Any,
    query: str,
    *,
    channel_id: str | None = None,
    team_id: str | None = None,
    action_token: str | None = None,
    user_token: str | None = None,
    limit: int = 8,
) -> tuple[list[ContextItem], RetrievalTrace]:
    trace = RetrievalTrace()
    items: list[ContextItem] = []
    seen: set[str] = set()

    def add(new_items: list[ContextItem]) -> int:
        added = 0
        for item in new_items:
            key = item.permalink or item.content
            if key in seen or not item.content:
                continue
            seen.add(key)
            items.append(item)
            added += 1
        return added

    if action_token:
        found = _rts_call(client, query, action_token=action_token, channel_id=channel_id)
        trace.rts_bot = add(found)
        log(f"RTS bot-token query={query!r} results={trace.rts_bot}")

    user_token = user_token or os.environ.get("SLACK_USER_TOKEN") or None
    if user_token and len(items) < limit:
        from slack_sdk import WebClient

        found = _rts_call(WebClient(token=user_token), query, channel_id=channel_id)
        trace.rts_user = add(found)
        log(f"RTS user-token query={query!r} results={trace.rts_user}")

    if len(items) < limit and channel_id:
        found, scanned = _history_scan(client, query, channel_id, team_id, limit=limit)
        trace.history = add(found)
        trace.channels_scanned = scanned
        log(f"history fallback channels={','.join(scanned)} results={trace.history}")

    return items[:limit], trace


def _rts_call(
    client: Any,
    query: str,
    *,
    action_token: str | None = None,
    channel_id: str | None = None,
) -> list[ContextItem]:
    payload: dict[str, Any] = {
        "query": query,
        "channel_types": ["public_channel", "private_channel", "mpim", "im"],
        "content_types": ["messages"],
        "include_context_messages": True,
        "limit": 8,
        "sort": "timestamp",
        "sort_dir": "desc",
    }
    if action_token:
        payload["action_token"] = action_token
    if channel_id:
        payload["context_channel_id"] = channel_id
    try:
        if hasattr(client, "assistant_search_context"):
            response = client.assistant_search_context(**payload)
        else:
            response = client.api_call("assistant.search.context", json=payload)
    except Exception as exc:  # noqa: BLE001 - RTS failure falls through to history
        log(f"RTS call failed: {exc}")
        return []

    results = response.get("results", {}) if isinstance(response, dict) or hasattr(response, "get") else {}
    messages = (results or {}).get("messages", []) or []
    items = []
    for message in messages:
        content = _clean(message.get("content") or message.get("text") or "")
        if not content:
            continue
        items.append(
            ContextItem(
                title=message.get("channel_name") or message.get("author_name") or "Slack message",
                content=content,
                permalink=message.get("permalink"),
                source="rts",
            )
        )
    return items


def _history_scan(
    client: Any,
    query: str,
    seed_channel_id: str,
    team_id: str | None,
    limit: int,
) -> tuple[list[ContextItem], list[str]]:
    channels: list[tuple[str, str]] = [(seed_channel_id, "current channel")]
    try:
        payload: dict[str, Any] = {"types": "public_channel", "exclude_archived": True, "limit": 100}
        if team_id:
            payload["team_id"] = team_id
        response = client.conversations_list(**payload)
        for channel in response.get("channels", []):
            if channel.get("id") != seed_channel_id and channel.get("is_member"):
                channels.append((channel["id"], f"#{channel.get('name', channel['id'])}"))
    except Exception as exc:  # noqa: BLE001
        log(f"conversations.list failed: {exc}")

    terms = _terms(query)
    scored: list[tuple[float, ContextItem]] = []
    scanned = []
    for channel_id, name in channels[:5]:
        scanned.append(name)
        try:
            history = client.conversations_history(channel=channel_id, limit=40)
        except Exception as exc:  # noqa: BLE001
            log(f"conversations.history failed for {name}: {exc}")
            continue
        for message in history.get("messages", []):
            if message.get("subtype"):
                continue
            text = _clean(message.get("text") or "")
            if len(text) < 20:
                continue
            score = sum(1 for t in terms if t in text.lower())
            if score <= 0:
                continue
            scored.append(
                (
                    score + float(message.get("ts", 0)) * 1e-12,
                    message["ts"],
                    channel_id,
                    ContextItem(title=f"recent message in {name}", content=text),
                )
            )
    scored.sort(key=lambda entry: entry[0], reverse=True)
    top = scored[:limit]
    for _, ts, chan, item in top:  # permalinks only for what we return — rate-limit friendly
        try:
            item.permalink = client.chat_getPermalink(channel=chan, message_ts=ts).get("permalink")
        except Exception:  # noqa: BLE001 - permalink is nice-to-have
            pass
    return [item for _, _, _, item in top], scanned


def _terms(query: str) -> list[str]:
    stopwords = {"what", "with", "this", "that", "week", "find", "minutes", "about", "meeting", "the", "and", "for"}
    return [t for t in re.findall(r"[a-z0-9_-]+", query.lower()) if len(t) > 3 and t not in stopwords]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
