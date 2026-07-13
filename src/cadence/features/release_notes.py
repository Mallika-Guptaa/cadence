"""Release Notes Compiler — turn a week of ship-talk into a customer changelog.

Scans recently cached workspace messages for ship-worthy language, buckets
each into Added / Fixed / Improved / Removed (first matching category wins),
cleans the text into short customer-facing bullets, optionally polishes the
wording via the LLM (deterministic fallback: the cleaned bullets, never
inventing items), and publishes the compiled markdown to CHANGELOG.md via the
MCP tool `publish_changelog`.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cadence import features, llm, style

KIND = "release_notes"
KEYWORDS = ("release notes", "changelog", "compile the release", "compile release")

TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))

MAX_PER_CATEGORY = 10
CATEGORY_ORDER = ("Added", "Fixed", "Improved", "Removed")
CATEGORY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Added", re.compile(r"\b(shipped|released|launched)\b", re.IGNORECASE)),
    ("Fixed", re.compile(r"\b(fixed|hotfix|resolved|bug)\b", re.IGNORECASE)),
    ("Improved", re.compile(r"\b(improved|optimized|refactored|faster)\b", re.IGNORECASE)),
    ("Removed", re.compile(r"\b(deprecated|removed)\b", re.IGNORECASE)),
)

_MENTION = re.compile(r"<[@!][^>]*>")
_CHANNEL = re.compile(r"<#[^>|]+(?:\|([^>]*))?>")
_LINK_LABELLED = re.compile(r"<[^>|]+\|([^>]*)>")
_LINK_BARE = re.compile(r"<(?:https?|mailto)[^>]*>")
_FORMATTING = re.compile(r"[*_~`]")
_LIST_MARKER = re.compile(r"^\s*(?:\d+[.)]\s*|[-•*]\s*)")


def _window_days(text: str) -> int:
    lowered = text.lower()
    if "last week" in lowered:
        return 14
    return 7


def _clean(raw: str) -> str:
    """Strip Slack mrkdwn/mentions/links into one short customer-facing line."""
    text = _MENTION.sub(" ", raw or "")
    text = _CHANNEL.sub(lambda m: m.group(1) or " ", text)
    text = _LINK_LABELLED.sub(lambda m: m.group(1), text)
    text = _LINK_BARE.sub(" ", text)
    text = _FORMATTING.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" \t-–—:;,.")
    if text:
        text = text[0].upper() + text[1:]
    return style.truncate(text, 140)


def _categorize(raw: str) -> str | None:
    # commitments are plans, not shipped work — "we'll fix X next sprint" isn't a Fixed item
    if re.search(r"\b(?:i['’]ll|we['’]ll|i will|we will|going to|next sprint|planning to)\b", raw, re.IGNORECASE):
        return None
    for name, pattern in CATEGORY_RULES:
        if pattern.search(raw):
            return name
    return None


def _collect(rows) -> dict[str, list[str]]:
    categories: dict[str, list[str]] = {name: [] for name in CATEGORY_ORDER}
    seen: set[str] = set()
    for row in reversed(rows):  # oldest first reads like a changelog
        raw = row["text"] or ""
        category = _categorize(raw)
        if category is None:
            continue
        bullet = _clean(raw)
        if not bullet or bullet.lower() in seen or len(categories[category]) >= MAX_PER_CATEGORY:
            continue
        seen.add(bullet.lower())
        categories[category].append(bullet)
    return {name: items for name, items in categories.items() if items}


def _polish(categories: dict[str, list[str]]) -> dict[str, list[str]]:
    """One LLM pass to make bullets customer-facing; fallback = cleaned bullets."""
    flat = [bullet for name in CATEGORY_ORDER for bullet in categories.get(name, [])]
    if not flat:
        return categories
    numbered = "\n".join(f"{i}. {bullet}" for i, bullet in enumerate(flat, 1))
    drafted = llm.draft(
        "Rewrite these internal engineering ship notes as short customer-facing "
        "changelog bullets. Return exactly one line per input, in the same order, "
        "plain text, no numbering, and never add items that are not in the input.",
        numbered,
    )
    if not drafted:
        return categories
    lines = [
        _LIST_MARKER.sub("", line).strip()
        for line in drafted.splitlines()
        if line.strip()
    ]
    if len(lines) != len(flat) or any(not line for line in lines):
        return categories
    polished: dict[str, list[str]] = {}
    cursor = 0
    for name in CATEGORY_ORDER:
        count = len(categories.get(name, []))
        if count:
            polished[name] = [style.truncate(line, 140) for line in lines[cursor:cursor + count]]
            cursor += count
    return polished


def _markdown(categories: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for name in CATEGORY_ORDER:
        items = categories.get(name)
        if not items:
            continue
        lines.append(f"### {name}")
        lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def _publish(ctx: features.FeatureContext, markdown: str, version: str) -> tuple[str | None, str | None]:
    """Publish via MCP; returns (path, error) — exactly one is set."""
    try:
        client = ctx.mcp() if callable(ctx.mcp) else None
        if client is None:
            return None, "MCP client unavailable"
        result = client.call_tool("publish_changelog", {"markdown": markdown, "version": version})
    except Exception as exc:  # noqa: BLE001 - publishing must not sink the card
        return None, str(exc)
    if isinstance(result, dict):
        return str(result.get("path") or "CHANGELOG.md"), None
    if isinstance(result, str) and result:
        return result, None
    return "CHANGELOG.md", None


def handle(text: str, ctx: features.FeatureContext) -> tuple[list[dict], str]:
    try:
        now = ctx.now or datetime.now(TZ)
        days = _window_days(text)
        since = now - timedelta(days=days)
        features.sync_channels(ctx)
        rows = ctx.store.messages_since(since.timestamp(), 400)
        categories = _collect(rows)
        if not categories:
            message = f"Nothing shippable found in the last {days} days."
            return [style.section(f":package: {message}")], message

        categories = _polish(categories)
        markdown = _markdown(categories)
        since_label = since.strftime("%b %-d, %Y")
        version = f"Week of {since.date().isoformat()}"
        path, error = _publish(ctx, markdown, version)

        budget = style.bullet_budget(text)
        blocks = [style.header(f"Release notes — week of {since_label}")]
        for name in CATEGORY_ORDER:
            items = categories.get(name)
            if items:
                blocks.append(style.section(f"*{name}*\n{style.bullets(items, budget)}"))
        if error:
            blocks.append(style.context(
                f":warning: Could not publish via MCP `publish_changelog` — {style.truncate(error, 120)}"
            ))
        else:
            blocks.append(style.context(
                f"Published to CHANGELOG.md via MCP `publish_changelog` — {path}"
            ))
        total = sum(len(items) for items in categories.values())
        return blocks, f"Release notes — week of {since_label} ({total} items)"
    except Exception as exc:  # noqa: BLE001 - handle() must never raise
        return (
            [style.section(f":warning: Release notes compiler hit an error: {style.truncate(str(exc), 200)}")],
            "Release notes failed.",
        )
