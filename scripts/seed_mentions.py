"""Seed a few messages that @-mention YOU, so Catch-Me-Up's "You were mentioned"
section is populated in the demo.

    python scripts/seed_mentions.py

Resolves your user id from SLACK_USER_TOKEN (the human account), then posts a
handful of persona messages across channels that tag you. Safe to re-run
(skips if the marker already exists).
"""

from __future__ import annotations

import os
import random
import sys
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

MARKER = "when you're back could you"
TAGGED = [
    ("cadence-engineering", "Priya Sharma", ":woman-technologist:",
     "{u} when you're back could you review the kubernetes ingress PR? It's blocking the release."),
    ("cadence-payments", "Marco Rossi", ":man-office-worker:",
     "{u} we need your call on the payments migration cutover window — pinging you so it's not missed."),
    ("cadence-incidents", "Grace Obi", ":woman-astronaut:",
     "{u} tagging you on the gateway 502 postmortem — your context on the retry logic would help."),
    ("cadence-product", "Dana Kim", ":woman-scientist:",
     "{u} the enterprise tier packaging doc needs your sign-off before Thursday's review."),
    ("cadence-data", "Sam Patel", ":man-technologist:",
     "{u} quick one for when you're back — is the metrics backfill safe to run over the weekend?"),
]


def main() -> None:
    bot = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    user_token = os.environ.get("SLACK_USER_TOKEN")
    if not user_token:
        raise SystemExit("SLACK_USER_TOKEN required to resolve your user id")
    uid = WebClient(token=user_token).auth_test()["user_id"]
    print(f"tagging user <@{uid}>")

    channels = {c["name"]: c["id"] for c in bot.conversations_list(types="public_channel", limit=200)["channels"]}

    posted = 0
    for name, persona, icon, template in TAGGED:
        cid = channels.get(name)
        if not cid:
            print(f"skip #{name} (not found)")
            continue
        hist = bot.conversations_history(channel=cid, limit=50).get("messages", [])
        if any(MARKER in (m.get("text") or "") for m in hist):
            print(f"#{name} already has a mention, skipping")
            continue
        try:
            bot.chat_postMessage(channel=cid, text=template.format(u=f"<@{uid}>"),
                                 username=persona, icon_emoji=icon)
            posted += 1
        except SlackApiError as exc:
            print(f"#{name}: {exc.response['error']}")
    print(f"posted {posted} messages tagging you")


if __name__ == "__main__":
    main()
