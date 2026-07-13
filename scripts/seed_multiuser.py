"""Multi-user, multi-channel live seeding: 10 personas, themed channels,
threaded conversations — promises, decisions, ship notes, questions.

    python scripts/seed_multiuser.py [conversations_per_channel=8]

Requires bot scopes channels:manage + chat:write.customize (in manifest).
Safe to re-run: it appends more conversation; personas rotate randomly.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

PERSONAS = [
    ("Priya Sharma", ":woman-technologist:"),
    ("Marco Rossi", ":man-office-worker:"),
    ("Dana Kim", ":woman-scientist:"),
    ("Sam Patel", ":man-technologist:"),
    ("Yuki Tanaka", ":woman-office-worker:"),
    ("Omar Farouk", ":man-scientist:"),
    ("Lena Novak", ":woman-artist:"),
    ("Ravi Iyer", ":man-cook:"),
    ("Grace Obi", ":woman-astronaut:"),
    ("Tom Becker", ":man-mechanic:"),
]

# channel -> topics discussed there
CHANNELS = {
    "cadence-engineering": ["kubernetes ingress", "api rate limiting", "feature flag cleanup", "ci pipeline speed"],
    "cadence-payments": ["payments migration", "double-charge retries", "checkout redirect flow", "invoice webhooks"],
    "cadence-infra": ["postgres partitioning", "redis eviction policy", "terraform module split", "on-call alert noise"],
    "cadence-incidents": ["gateway 502 spike", "cert expiry outage", "queue backlog incident", "search latency regression"],
    "cadence-design": ["onboarding redesign", "dark mode tokens", "empty-state illustrations", "mobile nav patterns"],
    "cadence-data": ["dashboard query engine", "event schema v2", "GDPR retention jobs", "metrics backfill"],
    "cadence-support": ["SSO SAML setup", "csv export requests", "billing portal confusion", "api key rotation questions"],
    "cadence-product": ["q3 roadmap", "enterprise tier packaging", "beta feedback themes", "pricing page rewrite"],
}

OPENERS = [
    "Kicking off a thread on {t} — current state and what's left before we can call it done.",
    "Anyone have context on {t}? Seeing something odd in staging since yesterday.",
    "Status on {t}: main work landed, two follow-ups remain.",
    "Heads up team — {t} changes go out behind a flag this week.",
    "We need a decision on {t} by Friday. Options are in the doc.",
    "Postmortem notes for {t} are drafted, please review before the sync.",
    "Customer feedback on {t} keeps coming up in tickets — collecting themes here.",
    "Shipped the first slice of {t} today. Watching the dashboards closely.",
]
REPLIES = [
    "I'll take the follow-up on {t} and send notes by friday.",
    "We'll fix the remaining {t} edge cases next sprint.",
    "Fixed the flaky part of {t} this morning — root cause was a stale cache.",
    "Improved {t} performance by roughly 30 percent with the batching change.",
    "We discussed {t} back in the spring — decision was to keep the current approach.",
    "I can pair on {t} tomorrow if that helps.",
    "Added docs for {t} to the wiki, link in the channel topic.",
    "The metrics for {t} look stable after the change, no regressions so far.",
    "Let's bring {t} to the team sync — I'll add it to the agenda.",
    "Good catch — filing a ticket for the {t} issue now.",
]
STANDALONE = [
    "Reminder: demo day is coming up, get your {t} updates in by thursday.",
    "Released the {t} improvements to all workspaces today.",
    "New runbook for {t} is live — feedback welcome.",
    "I owe the team a summary on {t}, will share it tomorrow.",
]


def main() -> None:
    convos_per_channel = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    bot = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    rng = random.Random()

    existing = {c["name"]: c for c in bot.conversations_list(types="public_channel", limit=200)["channels"]}
    channel_ids: dict[str, str] = {}
    for name in CHANNELS:
        if name in existing:
            channel_ids[name] = existing[name]["id"]
            if not existing[name].get("is_member"):
                bot.conversations_join(channel=existing[name]["id"])
        else:
            try:
                created = bot.conversations_create(name=name)
                channel_ids[name] = created["channel"]["id"]
                print(f"created #{name}")
            except SlackApiError as exc:
                raise SystemExit(
                    f"cannot create #{name}: {exc.response['error']} — check channels:manage scope + reinstall"
                )

    def post(cid: str, text: str, persona, thread_ts: str | None = None) -> str:
        name, icon = persona
        while True:
            try:
                r = bot.chat_postMessage(
                    channel=cid, text=text, username=name, icon_emoji=icon, thread_ts=thread_ts
                )
                return r["ts"]
            except SlackApiError as exc:
                if exc.response["error"] == "missing_scope":
                    raise SystemExit("missing_scope: chat:write.customize not granted — reinstall the app")
                if exc.response["error"] == "ratelimited":
                    time.sleep(int(exc.response.headers.get("Retry-After", 3)))
                    continue
                raise

    total = 0
    for name, cid in channel_ids.items():
        topics = CHANNELS[name]
        for _ in range(convos_per_channel):
            topic = rng.choice(topics)
            speakers = rng.sample(PERSONAS, k=min(4, len(PERSONAS)))
            if rng.random() < 0.25:  # standalone announcement
                post(cid, rng.choice(STANDALONE).format(t=topic), speakers[0])
                total += 1
            else:  # threaded conversation: opener + 2-3 replies from others
                parent = post(cid, rng.choice(OPENERS).format(t=topic), speakers[0])
                total += 1
                for replier in speakers[1 : rng.randint(3, 4)]:
                    post(cid, rng.choice(REPLIES).format(t=topic), replier, thread_ts=parent)
                    total += 1
                    time.sleep(0.3)
            time.sleep(0.3)
        print(f"#{name}: seeded")
    print(f"posted {total} messages as {len(PERSONAS)} personas across {len(channel_ids)} channels")


if __name__ == "__main__":
    main()
