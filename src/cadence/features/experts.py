"""Who-Knows-What: expertise router.

Answers "who knows about X?" by mining the cached channel history. The FTS5
expertise query counts how often each person discussed the topic terms; the
top people (excluding the asker) are shown with hit counts, last-active dates
and a sample-message link, followed by a ready-to-send intro the asker can
copy. The intro is optionally polished by the LLM with a full deterministic
template fallback.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from cadence import features, style
from cadence.llm import draft

KIND = "who_knows"
KEYWORDS = ("who knows", "who can help", "who understands", "expert on", "expert in")

TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))

_PREPOSITIONS = r"about|with|on|in"

STOPWORDS = frozenset(
    "the and for with about this that from our your their his her its who what when where how why "
    "can could should would will know knows understands understand help helps helping anyone someone "
    "somebody please need needs some any all has have had was were are is not you they them there "
    "here get got like just really very much more most does doing did into onto over under been "
    "being also expert experts stuff thing things".split()
)


def _extract_topic(text: str) -> str:
    """Topic = text after 'about/with/on/in', else after the keyword phrase."""
    lowered = text.lower()
    tail = lowered
    for keyword in KEYWORDS:
        idx = lowered.find(keyword)
        if idx != -1:
            tail = lowered[idx + len(keyword):]
            break
    match = re.search(rf"\b(?:{_PREPOSITIONS})\s+(.+)", tail)
    if match:
        tail = match.group(1)
    tail = re.sub(r"[^\w\s-]", " ", tail)
    return re.sub(r"\s+", " ", tail).strip()


def _terms(topic: str) -> list[str]:
    return [w for w in re.findall(r"[\w-]{3,}", topic.lower()) if w not in STOPWORDS]


def _intro_draft(expert_name: str, asker: str, topic: str) -> str:
    template = (
        f"Hi {expert_name} — {asker} is looking for help with {topic}; you've discussed it "
        "recently — could you point them in the right direction?"
    )
    polished = draft(
        "You polish short Slack intro messages. Return only the message text, friendly and "
        "direct, keeping every name and the topic intact." + style.CONCISE_LLM_SUFFIX,
        template,
    )
    return (polished or template).strip()


def _expert_line(ctx: features.FeatureContext, row: Any) -> str:
    name = row["user_name"] or row["user_id"] or "someone"
    hits = row["hits"]
    last = datetime.fromtimestamp(row["last_seen"], TZ).strftime("%b %-d, %Y")
    url = features.permalink(ctx.client, row["channel_id"], row["sample_ts"])
    plural = "s" if hits != 1 else ""
    return f"*{name}* — {hits} message{plural} on this, last active {last} · {style.link(url, 'view a sample')}"


def _handle(text: str, ctx: features.FeatureContext) -> tuple[list[dict], str]:
    topic = _extract_topic(text)
    terms = _terms(topic)
    if not terms:
        message = ":warning: Tell me a topic — e.g. `who knows about kubernetes ingress?`"
        return [style.section(message)], "Who-knows needs a topic."

    features.sync_channels(ctx)
    shown_limit = 6 if style.wants_detail(text) else 3
    rows = ctx.store.expertise_for(terms, limit=shown_limit + 1)
    others = [r for r in rows if not (ctx.user_id and r["user_id"] == ctx.user_id)]
    # solo-sandbox honesty: if only the asker has discussed it, show that rather than "no one"
    asker_only = not others and bool(rows)
    experts = (others or list(rows))[:shown_limit]

    if not experts:
        message = f"No one has discussed *{topic}* in the channels I can see — try #general?"
        return [style.section(message)], f"No experts found for {topic}."

    blocks: list[dict] = [style.header(f"Who knows about {topic}")]
    for row in experts:
        blocks.append(style.section(_expert_line(ctx, row)))

    top = experts[0]
    top_name = top["user_name"] or top["user_id"] or "there"
    if asker_only:
        blocks.append(style.context(f"That's you, {top_name} — you're the one who's discussed *{topic}* here. :trophy:"))
    else:
        intro = _intro_draft(top_name, ctx.user_name, topic).replace("\n", "\n> ")
        blocks.append(style.divider())
        blocks.append(style.section(f"*Ready-to-send intro for {top_name}:*\n> {intro}"))
    blocks.append(style.context("Ranked by how often each person discussed it in cached channels."))
    fallback = f"Top expert on {topic}: {top_name} ({top['hits']} messages)."
    return blocks, fallback


def handle(text: str, ctx: features.FeatureContext) -> tuple[list[dict], str]:
    try:
        return _handle(text, ctx)
    except Exception as exc:  # noqa: BLE001 - features never raise out of handle()
        message = f":warning: Couldn't look up experts: {exc}"
        return [style.section(message)], message
