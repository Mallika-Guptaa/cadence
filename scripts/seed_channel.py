"""Seed the sandbox with realistic chatter so every Cadence feature has demo material.

    SLACK_BOT_TOKEN=... python scripts/seed_channel.py [channel-name]

Covers: meeting-topic context (agendas/handovers), commitments (Promise Keeper),
ship-worthy updates (Release Notes), topical expertise (Who-Knows-What), and
prior discussions (Déjà Vu). Skips if the marker message already exists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

MARKER = "Q3 launch planning kickoff"
MESSAGES = [
    # meeting/handover context
    "Q3 launch planning kickoff: target date is end of the month, docs and pricing page still open items.",
    "Update on the Q3 launch: design review went well, but we still need a decision on the rollout regions.",
    "Payments migration status: schema review is done, cutover rehearsal planned for Thursday. Standup covers blockers daily.",
    "Customer onboarding call prep: the Acme team asked about SSO setup and data import timelines.",
    "Reminder: Q3 launch planning review on Thursday - bring the open pricing questions.",
    # promises (Promise Keeper)
    "I'll send the updated pricing deck by Friday.",
    "We'll fix the checkout redirect bug next sprint.",
    "I will share the load test results tomorrow.",
    # ship-worthy updates (Release Notes)
    "Shipped the new checkout flow to 10% of traffic today.",
    "Fixed the double-charge bug in payments retries.",
    "Improved dashboard load time by 40% after the query engine migration.",
    # expertise chatter (Who-Knows-What)
    "For kubernetes ingress issues check the ingress-nginx values file, I documented the TLS setup there.",
    "The payments migration runbook is in the wiki - ping me about cutover steps anytime.",
    # prior-discussion fodder (Déjà Vu)
    "We discussed SSO setup for enterprise customers back in the spring - decision was to require SAML for tier-1 accounts.",
]


def main() -> None:
    channel_name = sys.argv[1] if len(sys.argv) > 1 else "general"
    # The bot token can read (channels:read, history); the user token can only
    # post (chat:write) — so resolve/read with the bot, post as the user so
    # Promise Keeper attribution and expertise mining look right in the demo.
    bot = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    user_token = os.environ.get("SLACK_USER_TOKEN")
    poster = WebClient(token=user_token) if user_token else bot
    print(f"seeding as {'user' if user_token else 'bot'}")

    channel_id = None
    for c in bot.conversations_list(types="public_channel", limit=200)["channels"]:
        if c["name"] == channel_name:
            channel_id = c["id"]
            break
    if not channel_id:
        raise SystemExit(f"channel #{channel_name} not found")

    history = bot.conversations_history(channel=channel_id, limit=100)
    if any(MARKER in (m.get("text") or "") for m in history["messages"]):
        print(f"#{channel_name} already seeded, skipping")
        return

    for text in MESSAGES:
        poster.chat_postMessage(channel=channel_id, text=text)
    print(f"seeded {len(MESSAGES)} messages into #{channel_name}")


if __name__ == "__main__":
    main()
