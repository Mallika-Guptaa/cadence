"""Deja Vu — duplicate-discussion detector.

On demand ("was this discussed before ... retro cadence?") it strips the
trigger phrase and stopwords from the request, full-text searches the cached
workspace history for PRIOR discussion (older than 24h), and shows the top
hits with dates, channels and permalinks. A hit only counts when at least two
distinct topic terms appear in it, so one-word overlaps never masquerade as
deja vu.

A passive scan hook can also drop a single threaded hint on a fresh message
that strongly (3+ terms) matches something older than 48h — but only when the
`live_scan` extra is set and a Slack client is available. When in doubt it
stays quiet.

Fully deterministic: no LLM calls, plain SQL + term matching.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from cadence import style
from cadence.features import FeatureContext, permalink, sync_channels

KIND = "deja_vu"
KEYWORDS = ("discussed before", "did we already", "was this discussed", "deja vu", "talked about this before")

TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))

DAY_SECONDS = 86400
SEARCH_POOL = 25          # fetch this many FTS hits before age/term filtering
MAX_TERMS = 6
MIN_TERMS_HANDLE = 2      # distinct terms a hit must contain to count as prior discussion
MIN_TERMS_PASSIVE = 3     # passive hook is stricter: never post when in doubt
PASSIVE_MIN_LENGTH = 80

# survives across requests, unlike ctx.extras which is rebuilt per event
_posted_hints: set[tuple] = set()
_posted_lock = threading.Lock()


def _min_age_seconds() -> int:
    """How old a hit must be to count as 'prior'. Env-tunable (read lazily so
    .env applies) — set CADENCE_DEJAVU_MIN_AGE_SECONDS=0 for same-day-seeded demos."""
    try:
        return int(os.environ.get("CADENCE_DEJAVU_MIN_AGE_SECONDS", str(DAY_SECONDS)))
    except ValueError:
        return DAY_SECONDS

STOPWORDS = frozenset(
    """a about above after again against all already also although always am an and any anyone anything are aren as at
    be because been before being but by can cannot come could did didn do does doesn doing don done during each else
    ever every for from get gets getting go going gonna got guys had has hasn have haven having he her here hers him
    his how i if in into is isn it its just know knows let lets like may maybe me might mine more most much must my
    need never new no not now of off on once only onto or other our ours out over own please really said same say says
    see seeing seen shall she should shouldn since so some someone something soon still such sure take than thanks
    that the their theirs them then there these they thing things think this those though thought to today tomorrow
    too under until up upon us very want wants was wasn way we well were weren what when where which while who whom
    why will with won would wouldn yes yesterday yet you your yours
    deja discuss discussed discussing discussion talk talked talking""".split()
)

_TRIGGER_PHRASES = tuple(sorted(KEYWORDS + ("déjà vu",), key=len, reverse=True))


def _topic_terms(text: str, cap: int = MAX_TERMS) -> list[str]:
    """Text minus trigger phrases & stopwords -> distinct search terms, in order."""
    lowered = (text or "").lower()
    for phrase in _TRIGGER_PHRASES:
        lowered = lowered.replace(phrase, " ")
    terms: list[str] = []
    for token in re.findall(r"[a-z0-9][a-z0-9_-]+", lowered):
        if len(token) >= 3 and token not in STOPWORDS and token not in terms:
            terms.append(token)
    return terms[:cap]


def _matched_terms(terms: list[str], hit_text: str) -> set[str]:
    lowered = (hit_text or "").lower()
    return {t for t in terms if t in lowered}


def _fmt_date(ts_num: float) -> str:
    return datetime.fromtimestamp(ts_num, TZ).strftime("%b %-d, %Y")


def handle(text: str, ctx: FeatureContext) -> tuple[list[dict], str]:
    """On-demand check: has this topic come up before (older than 24h)?"""
    try:
        now = ctx.now or datetime.now(TZ)
        terms = _topic_terms(text)
        if not terms:
            message = "Tell me the topic to check — e.g. `was this discussed before: retro cadence`."
            return [style.section(message)], "Deja Vu needs a topic to check."

        sync_channels(ctx)
        # even with min-age 0 (same-day demos), never match the last two minutes —
        # sync indexes fresh chatter (including questions) moments after it's posted
        cutoff = now.timestamp() - max(_min_age_seconds(), 120)
        # over-fetch, then filter: fresh chatter must not evict the genuine old discussion
        hits = ctx.store.search_messages(terms, limit=SEARCH_POOL, exclude_ts=None)
        prior = [
            row for row in hits
            if row["ts_num"] < cutoff and len(_matched_terms(terms, row["text"])) >= MIN_TERMS_HANDLE
        ][:5]
        if not prior:
            message = "No prior discussion found — looks like new ground. :seedling:"
            return (
                [style.section(message), style.context(f"topic: {', '.join(terms)}")],
                "No prior discussion found — looks like new ground.",
            )

        items = []
        for i, row in enumerate(prior):
            url = permalink(ctx.client, row["channel_id"], row["ts"]) if i < 3 else None
            channel = row["channel_name"] or row["channel_id"]
            items.append(f"{_fmt_date(row['ts_num'])} in {channel} — {style.link(url, style.truncate(row['text'], 90))}")
        effective = max(_min_age_seconds(), 120)
        age_note = "24h" if effective == DAY_SECONDS else (f"{effective // 60}m" if effective < 3600 else f"{effective // 3600}h")
        blocks = [style.header("This came up before")]
        summary = _summarize(prior, ", ".join(terms))
        if summary:
            blocks.append(style.section(f"*Summary* — {summary}"))
        blocks.append(style.section("*When & where*\n" + style.bullets(items, style.bullet_budget(text))))
        blocks.append(style.context(f"topic: {', '.join(terms)} · prior = older than {age_note}"))
        return blocks, f"This came up before — {len(prior)} prior discussion(s) about {', '.join(terms)}."
    except Exception as exc:  # noqa: BLE001 - handle() must never raise
        return [style.section(f":warning: Deja Vu check failed: {exc}")], "Deja Vu check failed."


def _summarize(prior: list, topic: str) -> str | None:
    """One-line synthesis of what the prior discussion concluded. LLM when
    available; deterministic fallback picks the message with the most
    decision/outcome signal."""
    from .. import llm

    sample = "\n".join(
        f"{_fmt_date(r['ts_num'])} — {r['user_name'] or 'someone'}: {style.truncate(r['text'], 180)}"
        for r in prior
    )
    drafted = llm.draft(
        f"These are earlier Slack messages about '{topic}'. In ONE sentence, say what was "
        "discussed and any decision or outcome reached. No preamble." + style.CONCISE_LLM_SUFFIX,
        sample,
    )
    if drafted:
        return drafted.strip().splitlines()[0].lstrip("•-* ").strip() or None
    # deterministic fallback: the most decision-like prior message
    decision_re = re.compile(r"\b(decid|decision|agreed|approv|final|conclud|chose|went with|outcome)\b", re.I)
    best = max(prior, key=lambda r: (bool(decision_re.search(r["text"] or "")), len(r["text"] or "")))
    return f"earlier discussion — e.g. \"{style.truncate(best['text'], 140)}\""


def scan_message(text: str, meta: dict, ctx: FeatureContext) -> None:
    """Passive hook: quietly hint in-thread when a long fresh message strongly
    matches a discussion older than 48h. Opt-in via ctx.extras['live_scan']."""
    try:
        if not ctx.extras.get("live_scan") or ctx.client is None or len(text or "") <= PASSIVE_MIN_LENGTH:
            return
        key = (meta.get("channel_id"), meta.get("ts"))
        with _posted_lock:
            if key in _posted_hints:
                return
            _posted_hints.add(key)  # claim before the slow work so a concurrent rescan can't double-post
        terms = _topic_terms(text)
        if len(terms) < MIN_TERMS_PASSIVE:
            return
        now = ctx.now or datetime.now(TZ)
        cutoff = now.timestamp() - max(2 * _min_age_seconds(), _min_age_seconds() + 60)
        hits = ctx.store.search_messages(terms, limit=SEARCH_POOL, exclude_ts=meta.get("ts"))
        strong = next(
            (row for row in hits
             if row["ts_num"] < cutoff and len(_matched_terms(terms, row["text"])) >= MIN_TERMS_PASSIVE),
            None,
        )
        if strong is None:
            return
        url = permalink(ctx.client, strong["channel_id"], strong["ts"])
        if not url:
            return
        ctx.client.chat_postMessage(
            channel=meta["channel_id"],
            thread_ts=meta["ts"],
            text=f":recycle: Psst — this may have been discussed before: {url}",
        )
    except Exception:  # noqa: BLE001 - passive hook must stay silent on any failure
        return
