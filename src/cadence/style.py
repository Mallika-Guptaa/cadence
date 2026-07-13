"""House style: concise by default, readable mrkdwn, expand only on request.

Every feature builds its cards through these helpers so the whole agent reads
as one product. Rule: answers fit on one screen unless the user asked for
detail ("details", "explain", "more", "verbose", "full").
"""

from __future__ import annotations

import re
from typing import Any

MAX_BULLETS_CONCISE = 4
MAX_BULLETS_DETAILED = 12

CONCISE_LLM_SUFFIX = (
    " Be concise: at most 3 short bullets or 2 sentences, no preamble, no headings, "
    "no closing remarks. Only expand if the user explicitly asked for detail."
)


def wants_detail(text: str) -> bool:
    return bool(re.search(r"\b(details?|explain|verbose|full|everything|elaborate|more info)\b", text.lower()))


def bullet_budget(text: str) -> int:
    return MAX_BULLETS_DETAILED if wants_detail(text) else MAX_BULLETS_CONCISE


# -- Block Kit helpers ---------------------------------------------------------

def header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text[:150]}}


def section(mrkdwn: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn[:2900]}}


def fields(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "type": "section",
        "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"[:2000] } for k, v in pairs[:10]],
    }


def context(mrkdwn: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn[:2900]}]}


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def bullets(items: list[str], budget: int = MAX_BULLETS_CONCISE) -> str:
    """Bulleted list, truncated with an honest '+N more'."""
    shown = [f"• {item}" for item in items[:budget]]
    if len(items) > budget:
        shown.append(f"_…and {len(items) - budget} more (ask for details)_")
    return "\n".join(shown)


def link(url: str | None, label: str) -> str:
    label = label.replace("|", "/").replace(">", "›").replace("<", "‹")  # keep the link token valid
    return f"<{url}|{label}>" if url else label


def clean_slack_markup(text: str) -> str:
    """Flatten raw Slack tokens so message text can nest safely inside cards
    and link labels: <@U…> -> @user, <#C…|name> -> #name, <url|label> -> label."""
    text = text or ""
    text = re.sub(r"<@([A-Z0-9]+)(?:\|([^>]*))?>", lambda m: "@" + (m.group(2) or "user"), text)
    text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<#([A-Z0-9]+)>", r"#channel", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]*)>", r"\2", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<!([a-z]+)(?:\|[^>]*)?>", r"@\1", text)  # @here/@channel stay inert text
    return text


def truncate(text: str, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", clean_slack_markup(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
