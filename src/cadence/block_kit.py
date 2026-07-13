"""Block Kit cards for Cadence's two flows."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .slots import CoverageProposal, Interval


def _fmt_slot(interval: Interval) -> str:
    start, end = interval.start, interval.end
    return f"{start.strftime('%a %b %-d')}, {start.strftime('%-H:%M')}–{end.strftime('%-H:%M')}"


def _fmt_event_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return dt.strftime("%a %b %-d, %-H:%M")


def slot_card(
    slots: list[Interval],
    attendee_names: list[str],
    attendee_emails: list[str],
    duration_minutes: int,
    topic: str,
) -> list[dict[str, Any]]:
    who = ", ".join(attendee_names)
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":calendar: I checked everyone's calendar. Best {duration_minutes}-minute slots for *{who}*:",
            },
        }
    ]
    if not slots:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":no_entry: No mutual free slot in that window. Try a different week or shorter meeting."},
            }
        )
        return blocks

    elements = []
    for i, slot in enumerate(slots):
        payload = {
            "start": slot.start.isoformat(),
            "end": slot.end.isoformat(),
            "emails": attendee_emails,
            "topic": topic,
        }
        elements.append(
            {
                "type": "button",
                "action_id": f"book_slot_{i}",
                "text": {"type": "plain_text", "text": _fmt_slot(slot)},
                "style": "primary" if i == 0 else None,
                "value": json.dumps(payload),
            }
        )
    # Slack rejects style: None — strip it
    for e in elements:
        if e.get("style") is None:
            e.pop("style", None)
    blocks.append({"type": "actions", "block_id": "slot_actions", "elements": elements})
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Click a slot to book it on everyone's calendar via the MCP calendar server."}],
        }
    )
    return blocks


def booking_confirmation(event: dict[str, Any], agenda_text: str, footer: str) -> list[dict[str, Any]]:
    attendees = ", ".join(a.split("@")[0].title() for a in event.get("attendees", []))
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: Booked *{event['title']}* — {_fmt_event_time(event['start'])}\n"
                    f"Attendees: {attendees}\nEvent `{event['id']}` created on all calendars (.ics artifact written)."
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📎 Agenda (auto-drafted from workspace context)*\n{agenda_text[:1500]}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer + " · booked via MCP tool `create_event`"}]},
    ]


def coverage_card(proposals: list[CoverageProposal], leave_label: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":palm_tree: Leave recorded for *{leave_label}*. Here's your coverage plan:",
            },
        }
    ]
    if not proposals:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "No meetings need cover in that window. Enjoy your leave! :tada:"}}
        )
        return blocks

    for i, proposal in enumerate(proposals):
        event = proposal.event
        when = _fmt_event_time(event["start"]) if isinstance(event["start"], str) else _fmt_slot(Interval(event["start"], event["end"]))
        leave_note = ""
        if proposal.on_leave:
            outs = ", ".join(e.split("@")[0].title() for e in proposal.on_leave)
            leave_note = f"\n_Skipped {outs} — also on leave that day._"
        if proposal.needs_manual_cover:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":warning: *{event['title']}* — {when}\nNobody free is available — needs manual cover.{leave_note}",
                    },
                }
            )
            continue
        best = proposal.best
        name = best["email"].split("@")[0].title()
        reason = f"free at that time, not on leave, {best['co_attendance']} shared meetings with you this week"
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{event['title']}* — {when}\nSuggested cover: *{name}* ({reason}){leave_note}",
                },
                "accessory": {
                    "type": "button",
                    "action_id": f"assign_cover_{i}",
                    "text": {"type": "plain_text", "text": f"Assign {name}"},
                    "style": "primary",
                    "value": json.dumps(
                        {
                            "event_id": event["id"],
                            "title": event["title"],
                            "to_email": best["email"],
                            "start": event["start"] if isinstance(event["start"], str) else event["start"].isoformat(),
                        }
                    ),
                },
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Assignments reassign the calendar event via MCP and post a handover brief."}],
        }
    )
    return blocks


def coverage_confirmation(event: dict[str, Any], substitute_email: str, brief: str, footer: str) -> list[dict[str, Any]]:
    name = substitute_email.split("@")[0].title()
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":handshake: *{event['title']}* reassigned to *{name}* "
                    f"({_fmt_event_time(event['start'])}). Calendars updated (`{event['id']}`)."
                ),
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*📎 Handover brief for {name}*\n{brief[:1500]}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer + " · reassigned via MCP tool `reassign_event`"}]},
    ]


def error_card(message: str) -> list[dict[str, Any]]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f":warning: {message}"}}]


def help_text() -> str:
    return (
        "Hi! I'm *Cadence* — your team's rhythm, kept inside Slack.\n"
        "*Time*\n"
        "• `Find 45 minutes for me, Priya and Marco this week`\n"
        "• `I'm on leave Thursday and Friday — find cover for my meetings`\n"
        "*Productivity*\n"
        "• `What did I miss in the last 2 days?` — personalized catch-up digest\n"
        "• `Show open promises` — commitments people made, with nudges\n"
        "• `What's the status of project Phoenix?` — cited status card\n"
        "• `Who knows about payments migration?` — expertise router\n"
        "• `Compile release notes` — publishable changelog from this week's chatter\n"
        "• `Was SSO setup discussed before?` — prior-discussion finder\n"
        "_Answers stay short by default — add \"details\" for the full version._"
    )
