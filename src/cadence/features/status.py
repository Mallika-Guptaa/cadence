"""Project Status Autopilot — one card that answers "how is project X?".

Pulls the authoritative picture from the project tracker over MCP
(`get_project`), cross-references recent Slack chatter from the message
cache, and renders a single traceable card: health, progress, blockers
(with ticket ids), risks, and linked Slack context. Fully deterministic —
every claim maps to a ticket id or a linked message; no LLM required.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from cadence import style
from cadence.features import FeatureContext, permalink, sync_channels

KIND = "project_status"
KEYWORDS = ("status of", "project status", "how is project", "status update on")

TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))

CHATTER_WINDOW_DAYS = 30
SLACK_SHOWN = 3

_HEALTH = {
    "on_track": (":large_green_circle:", "On track"),
    "at_risk": (":large_yellow_circle:", "At risk"),
    "blocked": (":red_circle:", "Blocked"),
}
_DONE_STATUSES = {"done", "closed", "complete", "completed", "resolved", "shipped", "merged"}
_LEAD_FILLERS = {"the", "a", "an", "our", "of", "on", "for", "project"}
_TAIL_FILLERS = {
    "project", "status", "doing", "going", "coming", "along", "please",
    "today", "now", "lately", "these", "days", "looking",
}
_STOPWORDS = {
    "the", "this", "that", "with", "from", "into", "over", "have", "will",
    "project", "team", "work", "and", "for", "our", "their", "them", "about",
}
_NAME_PATTERNS = (
    re.compile(r"\bstatus\s+(?:update\s+)?(?:of|on|for)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bhow\s+is\s+(?:the\s+)?project\s+(.+)", re.IGNORECASE),
    re.compile(r"\bproject\s+(.+?)\s+status\b", re.IGNORECASE),
    re.compile(r"\bproject\s+status\b[:\s]*(.*)", re.IGNORECASE),
)


def handle(text: str, ctx: FeatureContext) -> tuple[list[dict], str]:
    try:
        return _handle(text, ctx)
    except Exception as exc:  # noqa: BLE001 - never raise out of a feature
        message = f":warning: Project status failed: {style.truncate(str(exc), 200)}"
        return [style.section(message)], message


def _handle(text: str, ctx: FeatureContext) -> tuple[list[dict], str]:
    now = ctx.now or datetime.now(TZ)
    mcp_client = _mcp(ctx)
    if mcp_client is None:
        message = ":warning: The project tracker (MCP) is not reachable right now."
        return [style.section(message)], "Project tracker unavailable"

    candidate = _extract_name(text)
    try:
        names = _project_names(mcp_client.call_tool("list_projects", {}))
    except Exception:  # noqa: BLE001 - fall back to the raw candidate below
        names = []

    name: str | None = None
    if candidate:
        name = _resolve(candidate, names) or (candidate if not names else None)
    if name is None:
        return _unknown_project_card(candidate, names, text)

    try:
        data = mcp_client.call_tool("get_project", {"name": name})
    except Exception as exc:  # noqa: BLE001 - MCP failure must name the problem
        message = (
            f":warning: MCP `get_project` failed for *{name}*: "
            f"{style.truncate(str(exc), 200)}"
        )
        return [style.section(message)], f"Could not fetch project {name} from the tracker"
    if not isinstance(data, dict) or not data:
        return _unknown_project_card(name, names, text)

    display = str(data.get("name") or name)
    tickets = [t for t in data.get("tickets") or [] if isinstance(t, dict)]
    done = sum(1 for t in tickets if str(t.get("status") or "").casefold() in _DONE_STATUSES)
    blocked = [
        t for t in tickets
        if t.get("blocker") or str(t.get("status") or "").casefold() == "blocked"
    ]
    health_key = re.sub(r"[\s-]+", "_", str(data.get("health") or "").strip().casefold())
    emoji, label = _HEALTH.get(health_key, (":white_circle:", health_key or "unknown"))
    budget = style.bullet_budget(text)

    sync_channels(ctx)
    hits: list[Any] = []
    try:
        terms = re.findall(r"[\w-]+", display) + _significant(str(data.get("description") or ""))
        hits = ctx.store.search_messages(
            terms, limit=5, since_ts=now.timestamp() - CHATTER_WINDOW_DAYS * 86400
        )
    except Exception:  # noqa: BLE001 - chatter is nice-to-have
        hits = []
    shown = hits[:SLACK_SHOWN]
    slack_items = []
    for row in shown:
        url = permalink(ctx.client, row["channel_id"], row["ts"]) or row["permalink"]
        who = row["user_name"] or "someone"
        slack_items.append(
            f"{style.link(url, row['channel_name'] or 'Slack')} {who}: "
            f"{style.truncate(row['text'], 110)}"
        )

    blocker_items = [
        f"`{t.get('id', '?')}` {style.truncate(str(t.get('title') or ''), 70)} — "
        f"_{style.truncate(str(t.get('blocker') or 'blocked'), 90)}_"
        for t in blocked
    ]
    risk_items = [style.truncate(_risk_text(r), 120) for r in data.get("risks") or []]

    blocks = [style.header(f"Project {display}")]
    desc = style.truncate(str(data.get("description") or ""), 140)
    blocks.append(style.section(f"{emoji} *{label}*" + (f" — {desc}" if desc else "")))
    blocks.append(style.fields([
        ("Progress", f"{done}/{len(tickets)} done"),
        ("Target", str(data.get("target_date") or "—")),
        ("Owner", str(data.get("owner") or "—")),
        ("Blocked", str(len(blocked))),
    ]))
    if blocker_items:
        blocks.append(style.section("*Blockers*\n" + style.bullets(blocker_items, budget)))
    if risk_items:
        blocks.append(style.section("*Risks*\n" + style.bullets(risk_items, budget)))
    if slack_items:
        blocks.append(style.section("*Latest from Slack*\n" + style.bullets(slack_items, SLACK_SHOWN)))
    blocks.append(style.context(
        f"Sources: project tracker via MCP `get_project` + {len(shown)} Slack messages"
    ))
    fallback = (
        f"Project {display}: {label} — {done}/{len(tickets)} done, {len(blocked)} blocked"
    )
    return blocks, fallback


# -- helpers -------------------------------------------------------------------

def _mcp(ctx: FeatureContext) -> Any | None:
    try:
        return ctx.mcp() if callable(ctx.mcp) else None
    except Exception:  # noqa: BLE001
        return None


def _extract_name(text: str) -> str | None:
    for pattern in _NAME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = re.sub(r"[\"'`“”‘’?!.,:;()]+", " ", match.group(1))
        words = raw.split()
        while words and words[0].lower() in _LEAD_FILLERS:
            words.pop(0)
        while words and words[-1].lower() in _TAIL_FILLERS:
            words.pop()
        if words:
            return " ".join(words[:5])
    return None


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _project_names(projects: Any) -> list[str]:
    names = []
    for item in projects or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _resolve(candidate: str, names: list[str]) -> str | None:
    target = _norm(candidate)
    if not target:
        return None
    for name in names:
        if _norm(name) == target:
            return name
    for name in names:
        normed = _norm(name)
        if target in normed or normed in target:
            return name
    return None


def _significant(description: str, limit: int = 6) -> list[str]:
    words: list[str] = []
    for word in re.findall(r"[A-Za-z][\w-]{3,}", description):
        lowered = word.lower()
        if lowered in _STOPWORDS or lowered in words:
            continue
        words.append(lowered)
        if len(words) >= limit:
            break
    return words


def _risk_text(risk: Any) -> str:
    if isinstance(risk, dict):
        return str(risk.get("text") or risk.get("description") or risk.get("title") or risk)
    return str(risk)


def _unknown_project_card(
    candidate: str | None, names: list[str], text: str
) -> tuple[list[dict], str]:
    if candidate:
        lead = f":grey_question: I couldn't find a project matching *{candidate}* in the tracker."
        fallback = f"Unknown project: {candidate}"
    else:
        lead = ":grey_question: Which project? I couldn't pick one out of that."
        fallback = "Which project?"
    blocks = [style.section(lead)]
    if names:
        blocks.append(style.section(
            "*Available projects*\n" + style.bullets(names, style.bullet_budget(text))
        ))
    else:
        blocks.append(style.context("The tracker returned no projects (MCP `list_projects`)."))
    return blocks, fallback
