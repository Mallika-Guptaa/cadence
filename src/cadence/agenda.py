"""Agenda and handover-brief drafting from retrieved workspace context.

LLM writes the prose when a key is available; the deterministic fallback still
produces a grounded, linked brief so RTS is demonstrable without any LLM.
"""

from __future__ import annotations

from typing import Any

from .context_search import ContextItem
from .llm import draft

AGENDA_SYSTEM = """You write crisp meeting agendas. Given a meeting topic, attendees, and
recent Slack messages about the topic, produce exactly three agenda bullets (each one line,
starting with '- ') followed by a line 'Related:' listing nothing (links are appended by the
caller). No preamble, no headings."""

HANDOVER_SYSTEM = """You write handover briefs for a colleague covering a meeting while the
owner is on leave. Given the meeting details and recent Slack messages about it, produce:
one sentence of context on what the meeting is about, then 2-3 bullets ('- ') of what the
substitute needs to know or do. No preamble, no headings."""


def draft_agenda(topic: str, attendees: list[str], items: list[ContextItem]) -> str:
    context = _context_block(items)
    if context:
        text = draft(
            AGENDA_SYSTEM,
            f"Topic: {topic or 'team sync'}\nAttendees: {', '.join(attendees)}\n\nRecent Slack context:\n{context}",
        )
        if text:
            return _append_links(text.strip(), items)
    # deterministic fallback: bullets straight from the evidence
    bullets = [f"- {item.content[:120]}" for item in items[:3]]
    if not bullets:
        bullets = [f"- Align on {topic or 'current priorities'}", "- Review open questions", "- Agree on next steps"]
    return _append_links("\n".join(bullets), items)


def draft_handover(event: dict[str, Any], substitute_name: str, items: list[ContextItem]) -> str:
    context = _context_block(items)
    if context:
        text = draft(
            HANDOVER_SYSTEM,
            f"Meeting: {event.get('title')} at {event.get('start')}\n"
            f"Substitute: {substitute_name}\n\nRecent Slack context:\n{context}",
        )
        if text:
            return _append_links(text.strip(), items)
    bullets = [f"- {item.content[:120]}" for item in items[:2]]
    if not bullets:
        bullets = ["- No recent workspace discussion found — check with the organizer before the meeting."]
    return _append_links(
        f"{substitute_name} is covering *{event.get('title')}*.\n" + "\n".join(bullets), items
    )


def _context_block(items: list[ContextItem]) -> str:
    return "\n".join(f"- {item.content[:300]}" for item in items[:6])


def _append_links(text: str, items: list[ContextItem]) -> str:
    links = [item.permalink for item in items[:3] if item.permalink]
    if links:
        text += "\nRelated: " + " ".join(f"<{link}|thread {i + 1}>" for i, link in enumerate(links))
    return text
