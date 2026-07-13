"""Promise Keeper — a commitment tracker.

Passively scans cached channel messages for first-person commitments
("I'll send the deck by Friday"), parses a due hint, and stores them as
promises. On request ("open promises", "who owes what") it renders a digest
card — overdue first — with per-promise Done / File task buttons. Filing a
task goes through the MCP `create_task` tool.

Fully deterministic: no LLM calls, so no fallback path is needed.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import style
from . import FeatureContext, log, permalink, sync_channels

KIND = "promises"
KEYWORDS = ("promise", "promises", "commitment", "i owe", "who owes")

TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))

MAX_MESSAGE_LEN = 300
EOD_HOUR = 17

_COMMIT_RE = re.compile(
    r"\b(?:i['’]ll|i will|we['’]ll|we will|i can|i['’]m going to|we['’]re going to)\s+[a-z]",
    re.IGNORECASE,
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


# -- passive scan --------------------------------------------------------------

def scan_message(text: str, meta: dict, ctx: FeatureContext) -> None:
    """Detect a first-person commitment in a channel message and track it."""
    try:
        if not text or len(text) >= MAX_MESSAGE_LEN:
            return
        if text.strip().endswith("?"):
            return
        if not _COMMIT_RE.search(text):
            return
        cleaned = re.sub(r"\s+", " ", text).strip()
        due_ts = _parse_due(cleaned, float(meta["ts"]))
        ctx.store.add_promise(
            meta.get("user_id"), meta.get("user_name"), cleaned, due_ts,
            meta.get("channel_id"), meta.get("ts"), meta.get("permalink"),
        )  # None on re-scan (UNIQUE dedupe) — that's fine
    except Exception as exc:  # noqa: BLE001 - a scan miss must never hurt the caller
        log(f"promises scan failed: {exc}")


def _parse_due(text: str, ref_epoch: float) -> float | None:
    """Due hint -> epoch seconds in TZ, relative to the message timestamp."""
    lowered = text.lower()
    ref = datetime.fromtimestamp(ref_epoch, TZ)
    for index, day in enumerate(_WEEKDAYS):
        if re.search(rf"\b{day}\b", lowered):
            ahead = (index - ref.weekday()) % 7 or 7
            return _at_eod(ref + timedelta(days=ahead))
    if re.search(r"\btomorrow\b", lowered):
        return _at_eod(ref + timedelta(days=1))
    if re.search(r"\bnext week\b", lowered):
        return _at_eod(ref + timedelta(days=7))
    if re.search(r"\bnext sprint\b", lowered):
        return _at_eod(ref + timedelta(days=14))
    if re.search(r"\b(?:eod|end of day)\b", lowered):
        due = _at_eod(ref)
        if due <= ref_epoch:  # "by EOD" said after 5pm means the next day, not born-overdue
            due = _at_eod(ref + timedelta(days=1))
        return due
    return None


def _at_eod(dt: datetime) -> float:
    return dt.replace(hour=EOD_HOUR, minute=0, second=0, microsecond=0).timestamp()


# -- digest: filter, dedupe, rank ---------------------------------------------

# "I can pair… if that helps" style offers are real but low-commitment — rank last
_VAGUE_RE = re.compile(
    r"\b(i can pair|if that helps|let'?s bring|i can cover|happy to help|feel free|"
    r"if (?:you|that|anyone)|ping me|around if)\b",
    re.IGNORECASE,
)
# concrete deliverables — a thing will actually be produced
_CONCRETE_RE = re.compile(
    r"\b(send|ship|deliver|finish|share|write|post|publish|prepare|draft|complete|"
    r"update|submit|review|merge|deploy|fix|file|schedule|set up|hand off)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def _priority(row, now_ts: float) -> tuple:
    """Lower tuple sorts first: overdue → due-soon → concrete → other → vague offers."""
    due = row["due_ts"]
    body = row["text"] or ""
    vague = bool(_VAGUE_RE.search(body))
    concrete = bool(_CONCRETE_RE.search(body))
    # a vague offer stays low even if it names a day ("pair tomorrow" ≠ a deadline)
    if vague:
        tier = 4
    elif due is not None and float(due) < now_ts:
        tier = 0                       # overdue
    elif due is not None:
        tier = 1                       # has a real deadline
    elif concrete:
        tier = 2                       # concrete deliverable, no date
    else:
        tier = 3
    due_key = float(due) if (due is not None and not vague) else float("inf")
    return (tier, due_key, row["created_ts"])


def _rank_and_dedupe(rows: list, now_ts: float) -> tuple[list, int]:
    """Sort by priority, then drop later duplicates of the same commitment text.

    Personas repeat templates ("I can pair on X…") across people; after ranking,
    the highest-priority instance of each distinct commitment is kept, the rest
    collapsed. Returns (deduped_rows, num_collapsed)."""
    ordered = sorted(rows, key=lambda r: _priority(r, now_ts))
    seen: set[str] = set()
    kept: list = []
    collapsed = 0
    for row in ordered:
        key = _norm(row["text"])
        if key and key in seen:
            collapsed += 1
            continue
        seen.add(key)
        kept.append(row)
    return kept, collapsed


def handle(text: str, ctx: FeatureContext) -> tuple[list[dict], str]:
    try:
        sync_channels(ctx)
        now = ctx.now or datetime.now(TZ)
        rows = ctx.store.open_promises()
        if not rows:
            return (
                [style.header("Open promises"),
                 style.section(":sparkles: No open promises — everyone is square.")],
                "No open promises",
            )
        now_ts = now.timestamp()
        overdue_ids = {r["id"] for r in rows if r["due_ts"] is not None and float(r["due_ts"]) < now_ts}
        ranked, collapsed = _rank_and_dedupe(rows, now_ts)
        shown = ranked[: style.bullet_budget(text)]

        blocks: list[dict] = [style.header("Open promises")]
        for row in shown:
            blocks.append(_row_section(row, row["id"] in overdue_ids, ctx))
            blocks.append(_row_actions(row))
        footer = f"{len(rows)} open · {len(overdue_ids)} overdue · sorted by priority"
        if collapsed:
            footer += f" · {collapsed} similar collapsed"
        if len(ranked) > len(shown):
            footer += f" · {len(ranked) - len(shown)} more (ask for details)"
        blocks.append(style.context(footer))
        return blocks, f"{len(rows)} open promises ({len(overdue_ids)} overdue)"
    except Exception as exc:  # noqa: BLE001 - never raise out of handle()
        return [style.section(f":warning: Couldn't build the promise digest: {exc}")], "Promise digest failed"


def _row_section(row: Any, is_overdue: bool, ctx: FeatureContext) -> dict:
    owner = row["owner_name"] or row["owner_id"] or "someone"
    parts = [f"*{owner}* — {style.truncate(row['text'], 120)}"]
    if row["due_ts"] is not None:
        parts.append(f"(due {_fmt_day(float(row['due_ts']))})")
    url = row["permalink"] or permalink(ctx.client, row["channel_id"], row["message_ts"])
    if url:
        parts.append(style.link(url, "link"))
    prefix = ":red_circle: " if is_overdue else ""
    return style.section(prefix + " ".join(parts))


def _row_actions(row: Any) -> dict:
    pid = row["id"]
    due_iso = datetime.fromtimestamp(float(row["due_ts"]), TZ).date().isoformat() if row["due_ts"] is not None else None
    task_payload = {
        "id": pid,
        "title": style.truncate(row["text"], 100),
        "owner": row["owner_name"] or row["owner_id"],
        "due": due_iso,
    }
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "action_id": f"promise_done_{pid}",
                "text": {"type": "plain_text", "text": "Done"},
                "style": "primary",
                "value": json.dumps({"id": pid}),
            },
            {
                "type": "button",
                "action_id": f"promise_task_{pid}",
                "text": {"type": "plain_text", "text": "File task"},
                "value": json.dumps(task_payload),
            },
        ],
    }


def _fmt_day(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, TZ)
    return f"{dt.strftime('%a %b')} {dt.day}"


# -- button actions --------------------------------------------------------------

def _on_done(payload: dict, ctx: FeatureContext) -> tuple[list[dict], str]:
    try:
        pid = int(payload["id"])
        row = ctx.store.get_promise(pid)
        if row is None:
            return [style.section(":warning: That promise is no longer tracked.")], "Promise not found"
        if row["status"] != "open":
            return (
                [style.section(f":information_source: Already handled — status is *{row['status']}*.")],
                "Already handled",
            )
        ctx.store.set_promise_status(pid, "done")
        owner = row["owner_name"] or row["owner_id"] or "someone"
        return (
            [style.section(f":white_check_mark: Done — *{owner}*: {style.truncate(row['text'], 120)}")],
            "Promise marked done",
        )
    except Exception as exc:  # noqa: BLE001
        return [style.section(f":warning: Couldn't mark that promise done: {exc}")], "Update failed"


def _on_task(payload: dict, ctx: FeatureContext) -> tuple[list[dict], str]:
    try:
        pid = int(payload["id"])
        row = ctx.store.get_promise(pid)
        if row is None:
            return [style.section(":warning: That promise is no longer tracked.")], "Promise not found"
        if row["status"] != "open":
            return (
                [style.section(f":information_source: Already handled — status is *{row['status']}*.")],
                "Already handled",
            )
        title = payload.get("title") or style.truncate(row["text"], 100)
        owner = payload.get("owner") or row["owner_name"] or row["owner_id"] or "unassigned"
        due = payload.get("due")
        if not due and row["due_ts"] is not None:
            due = datetime.fromtimestamp(float(row["due_ts"]), TZ).date().isoformat()
        due = due or ""  # MCP create_task requires a string, never None
        source = row["permalink"] or f"slack:{row['channel_id']}/{row['message_ts']}"
        try:
            result = ctx.mcp().call_tool(
                "create_task", {"title": title, "owner": owner, "due": due, "source": source}
            )
        except Exception as exc:  # noqa: BLE001 - MCP down must degrade to an error card
            return [style.section(f":warning: Couldn't file the task: {exc}")], "Task filing failed"
        ctx.store.set_promise_status(pid, "task_filed")
        task_id = result.get("id") or result.get("task_id") if isinstance(result, dict) else result
        label = f" `{task_id}`" if task_id else ""
        return (
            [style.section(f":card_index: Task{label} filed for *{owner}* — {style.truncate(title, 100)}")],
            f"Task{label} filed",
        )
    except Exception as exc:  # noqa: BLE001
        return [style.section(f":warning: Couldn't file that task: {exc}")], "Task filing failed"


ACTIONS = {"promise_done": _on_done, "promise_task": _on_task}
