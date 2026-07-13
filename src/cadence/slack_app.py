"""Cadence Slack app: Bolt + Assistant surface + action handlers.

Flow per request: extract intent (LLM/regex) -> calendar data via MCP tools ->
deterministic slot/coverage engine -> Block Kit card. Button clicks call MCP
create_event / reassign_event and post confirmations with RTS-grounded briefs.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from slack_bolt import App, Assistant
from slack_bolt.adapter.socket_mode import SocketModeHandler

from . import block_kit
from . import features as feature_registry
from .agenda import draft_agenda, draft_handover
from .context_search import search_context
from .features import FeatureContext
from .intent import (
    extract_intent,
    resolve_leave_dates,
    resolve_leave_window,
    resolve_meeting_window,
)
from .mcp_client import McpCalendarClient, McpError
from .slots import Interval, find_free_slots, match_coverage
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo(os.environ.get("SCHEDULER_TZ", "America/Los_Angeles"))
ME_EMAIL = os.environ.get("CADENCE_ME", "alex@cadence.demo")
LIVE_SCAN = os.environ.get("CADENCE_LIVE_SCAN", "") == "1"

SUGGESTED_PROMPTS = [
    {"title": "Find a meeting slot", "message": "Find 45 minutes for me, Priya and Marco this week"},
    {"title": "Cover my leave", "message": "I'm on leave Thursday and Friday — find cover for my meetings"},
    {"title": "Catch me up", "message": "What did I miss in the last 2 days?"},
    {"title": "Project status", "message": "What's the status of project Phoenix?"},
]

# verb + time-noun within a short span = unambiguous meeting ask
_SCHEDULING_PRIORITY_RE = re.compile(
    r"\b(find|schedule|book|grab)\b.{0,40}\b(minutes?|mins?|hours?|time|slot|meeting|sync)\b", re.IGNORECASE
)
_LEAVE_PRIORITY_RE = re.compile(r"\b(on leave|vacation|ooo|out of office|pto|cover for my)\b", re.IGNORECASE)

_mcp: McpCalendarClient | None = None
_store: Store | None = None
_user_names: dict[str, str] = {}
_bot_user_id: str | None = None
_singleton_lock = threading.Lock()
_handled_actions: set[str] = set()  # idempotency: one click, one side effect
_handled_lock = threading.Lock()


def log(message: str) -> None:
    print(f"[cadence] {message}", file=sys.stderr, flush=True)


def mcp() -> McpCalendarClient:
    global _mcp
    with _singleton_lock:
        if _mcp is None:
            _mcp = McpCalendarClient(str(ROOT / "mcp_server" / "calendar_tools.py"))
            log(f"MCP calendar server connected, tools: {', '.join(_mcp.list_tools())}")
        return _mcp


def store() -> Store:
    global _store
    with _singleton_lock:
        if _store is None:
            _store = Store()
        return _store


def _already_handled(action_id: str, value: str) -> bool:
    """True if this exact button click (id+payload) was already processed."""
    key = f"{action_id}:{value}"
    with _handled_lock:
        if key in _handled_actions:
            return True
        _handled_actions.add(key)
        return False


def _user_name(client, user_id: str | None) -> str:
    if not user_id or client is None:
        return "you"
    if user_id not in _user_names:
        try:
            info = client.users_info(user=user_id)["user"]
            _user_names[user_id] = info.get("real_name") or info.get("name") or user_id
        except Exception:  # noqa: BLE001
            _user_names[user_id] = user_id
    return _user_names[user_id]


def _feature_ctx(client=None, user_id=None, channel_id=None, team_id=None) -> FeatureContext:
    return FeatureContext(
        store=store(),
        mcp=mcp,
        client=client,
        user_id=user_id,
        user_name=_user_name(client, user_id),
        channel_id=channel_id,
        team_id=team_id,
        now=datetime.now(TZ),
        extras={"live_scan": LIVE_SCAN, "me_email": ME_EMAIL, "bot_user_id": _bot_user_id},
    )


def _iv(start: str, end: str) -> Interval:
    def parse(iso: str) -> datetime:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ)

    return Interval(parse(start), parse(end))


def _busy_intervals(busy: dict[str, list[dict]]) -> dict[str, list[Interval]]:
    return {email: [_iv(b["start"], b["end"]) for b in blocks] for email, blocks in busy.items()}


def directory() -> dict[str, str]:
    """name -> email for the demo team."""
    return {u["name"].lower(): u["email"] for u in mcp().call_tool("list_users", {})}


def handle_request(
    text: str,
    say,
    set_status=None,
    *,
    client=None,
    user_id: str | None = None,
    channel_id: str | None = None,
    team_id: str | None = None,
) -> None:
    if set_status:
        try:
            set_status("Reading your request…")
        except Exception:  # noqa: BLE001
            pass

    def dispatch_feature(module) -> None:
        if set_status:
            try:
                set_status("Working on it…")
            except Exception:  # noqa: BLE001
                pass
        ctx = _feature_ctx(client, user_id, channel_id, team_id)
        try:
            blocks, fallback = module.handle(text, ctx)
        except Exception as exc:  # noqa: BLE001 - a feature bug must not kill the listener
            log(f"feature {module.KIND} crashed: {exc}")
            blocks, fallback = block_kit.error_card(f"That feature hit a snag: {exc}"), "Feature error"
        say(blocks=blocks, text=fallback)

    # Routing precedence: (1) strong scheduling signals win — "find time to
    # discuss release notes" is a meeting ask, not a release-notes ask;
    # (2) feature keywords; (3) LLM intent for everything ambiguous. The
    # precedence regex is deliberately narrower than regex_intent's net, so
    # phrases like "SSO setup" can't hijack a feature request.
    module = None if _SCHEDULING_PRIORITY_RE.search(text) or _LEAVE_PRIORITY_RE.search(text) else feature_registry.route(text)
    if module is not None:
        log(f"feature route (keywords): {module.KIND}")
        dispatch_feature(module)
        return

    try:
        team = directory()
    except McpError as exc:
        # features don't need the calendar directory — let the LLM route them even with MCP down
        log(f"directory unavailable: {exc}")
        intent = extract_intent(text, [])
        if (module := feature_registry.by_kind(intent.kind)) is not None:
            dispatch_feature(module)
        else:
            say(blocks=block_kit.error_card(f"Calendar backend unavailable: {exc}"), text="Calendar backend unavailable")
        return

    intent = extract_intent(text, list(team.keys()))
    log(f"intent kind={intent.kind} attendees={intent.attendees} duration={intent.duration_minutes} leave_days={intent.leave_days}")
    now = datetime.now(TZ)

    if intent.kind == "schedule_meeting":
        _handle_meeting(intent, team, now, say, set_status)
    elif intent.kind == "leave_coverage":
        _handle_leave(intent, team, now, say, set_status)
    elif (module := feature_registry.by_kind(intent.kind)) is not None:
        log(f"feature route (LLM intent): {intent.kind}")
        dispatch_feature(module)
    else:
        say(text=block_kit.help_text())


def _handle_meeting(intent, team: dict[str, str], now: datetime, say, set_status) -> None:
    emails = {ME_EMAIL}
    names = ["You"]
    for name in intent.attendees:
        if name not in team:  # tolerate LLM truncations/typos: unique prefix match
            candidates = [k for k in team if k.startswith(name) or name.startswith(k)]
            if len(candidates) == 1:
                name = candidates[0]
        if name in team and team[name] != ME_EMAIL:
            emails.add(team[name])
            names.append(name.title())
    if len(emails) < 2:
        say(blocks=block_kit.error_card(
            f"I couldn't match the attendees to the team directory ({', '.join(n.title() for n in team)}). Who should I include?"
        ), text="Attendees not recognized")
        return

    if set_status:
        try:
            set_status("Checking everyone's calendars…")
        except Exception:  # noqa: BLE001
            pass

    window = resolve_meeting_window(intent, now)
    try:
        busy = mcp().call_tool(
            "get_availability",
            {"emails": sorted(emails), "start": window.start.isoformat(), "end": window.end.isoformat()},
        )
    except McpError as exc:
        say(blocks=block_kit.error_card(str(exc)), text="Calendar lookup failed")
        return

    slots = find_free_slots(_busy_intervals(busy), window, intent.duration_minutes)
    say(
        blocks=block_kit.slot_card(slots, names, sorted(emails), intent.duration_minutes, intent.topic),
        text=f"Found {len(slots)} slots",
    )


def _handle_leave(intent, team: dict[str, str], now: datetime, say, set_status) -> None:
    window = resolve_leave_window(intent, now)
    if window is None:
        say(blocks=block_kit.error_card("Which days are you on leave? e.g. _on leave Thursday and Friday_"), text="Which days?")
        return

    if set_status:
        try:
            set_status("Finding cover for your meetings…")
        except Exception:  # noqa: BLE001
            pass

    try:
        mcp().call_tool(
            "record_leave",
            {"email": ME_EMAIL, "start": window.start.isoformat(), "end": window.end.isoformat()},
        )
        events = mcp().call_tool(
            "get_events",
            {"email": ME_EMAIL, "start": window.start.isoformat(), "end": window.end.isoformat()},
        )
        leave_dates = set(resolve_leave_dates(intent, now))
        events = [
            e for e in events
            if e.get("kind") == "meeting"
            and datetime.fromisoformat(e["start"]).date() in leave_dates
        ]
        for event in events:
            event["start_dt"], event["end_dt"] = datetime.fromisoformat(event["start"]), datetime.fromisoformat(event["end"])

        candidates = {email for email in team.values() if email != ME_EMAIL}
        week = resolve_meeting_window(intent.model_copy(update={"window": "this_week"}), now)
        busy = mcp().call_tool(
            "get_availability",
            {"emails": sorted(candidates), "start": window.start.isoformat(), "end": window.end.isoformat()},
        )
        co_attendance = _co_attendance(candidates, week)
        leave_by_candidate = _candidate_leave(candidates, window)  # who else is out
    except McpError as exc:
        say(blocks=block_kit.error_card(str(exc)), text="Calendar lookup failed")
        return

    engine_events = [
        {**e, "start": e["start_dt"], "end": e["end_dt"]}
        for e in events
    ]
    proposals = match_coverage(
        ME_EMAIL, window, engine_events, _busy_intervals(busy), co_attendance, leave_by_candidate
    )
    for proposal in proposals:  # cards need ISO strings back
        proposal.event["start"] = proposal.event["start"].isoformat()
        proposal.event["end"] = proposal.event["end"].isoformat()

    label = f"{window.start.strftime('%a %b %-d')} – {window.end.strftime('%a %b %-d')}"
    say(blocks=block_kit.coverage_card(proposals, label), text="Coverage plan ready")


def _co_attendance(candidates: set[str], week: Interval) -> dict[str, int]:
    counts = {}
    for email in candidates:
        try:
            events = mcp().call_tool(
                "get_events",
                {"email": email, "start": week.start.isoformat(), "end": week.end.isoformat()},
            )
            counts[email] = sum(1 for e in events if ME_EMAIL in e.get("attendees", []))
        except McpError:
            counts[email] = 0
    return counts


_LEAVE_TITLE_RE = re.compile(r"\b(on leave|ooo|out of office|vacation|pto|holiday|off)\b", re.IGNORECASE)


def _candidate_leave(candidates: set[str], window: Interval) -> dict[str, list[Interval]]:
    """Leave/OOO blocks per candidate in the window — so we don't hand a meeting
    to someone who is themselves out."""
    out: dict[str, list[Interval]] = {}
    for email in candidates:
        blocks: list[Interval] = []
        try:
            events = mcp().call_tool(
                "get_events",
                {"email": email, "start": window.start.isoformat(), "end": window.end.isoformat()},
            )
            for e in events:
                if e.get("kind") == "leave" or _LEAVE_TITLE_RE.search(e.get("title", "")):
                    blocks.append(Interval(datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])))
        except McpError:
            pass
        if blocks:
            out[email] = blocks
    return out


def build_app() -> App:
    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    assistant = Assistant()

    @assistant.thread_started
    def greet(say, set_suggested_prompts):
        say(text=block_kit.help_text())
        set_suggested_prompts(prompts=SUGGESTED_PROMPTS)

    @assistant.user_message
    def on_assistant_message(payload, say, set_status, client, context):
        log(f"assistant message: {payload.get('text', '')!r}")
        handle_request(
            payload.get("text", ""), say, set_status,
            client=client, user_id=payload.get("user"),
            channel_id=payload.get("channel"), team_id=context.get("team_id"),
        )

    app.use(assistant)

    @app.middleware
    def remember_bot_user(context, next):
        global _bot_user_id
        if _bot_user_id is None and context.get("bot_user_id"):
            _bot_user_id = context["bot_user_id"]
        next()

    @app.event("app_mention")
    def on_mention(event, say, context, client):
        text = event.get("text", "")
        # strip the bot mention
        if context.get("bot_user_id"):
            text = text.replace(f"<@{context['bot_user_id']}>", "").strip()
        log(f"app_mention: {text!r} action_token={'yes' if event.get('action_token') else 'no'}")
        thread_ts = event.get("thread_ts") or event.get("ts")

        def threaded_say(**kwargs):
            say(thread_ts=thread_ts, **kwargs)

        handle_request(
            text, threaded_say,
            client=client, user_id=event.get("user"),
            channel_id=event.get("channel"), team_id=context.get("team_id"),
        )

    @app.command("/schedule")
    def on_command(ack, command, respond, client):
        ack()
        log(f"/schedule: {command.get('text', '')!r}")

        def respond_say(**kwargs):
            # response_url works even in channels the bot isn't a member of
            kwargs.pop("thread_ts", None)
            respond(response_type="in_channel", **kwargs)

        handle_request(
            command.get("text", ""), respond_say,
            client=client, user_id=command.get("user_id"),
            channel_id=command.get("channel_id"), team_id=command.get("team_id"),
        )

    @app.event("message")
    def on_message(event, say, client, context):
        subtype = event.get("subtype")
        if event.get("channel_type") == "im":
            if subtype or event.get("bot_id"):
                return
            handle_request(
                event.get("text", ""), say,
                client=client, user_id=event.get("user"),
                channel_id=event.get("channel"), team_id=context.get("team_id"),
            )
            return
        if event.get("channel_type") == "channel":
            # passive intelligence: cache the message and run feature scan hooks.
            # Keep bot_message posts (personas carry a username); drop other
            # subtypes, Cadence's own cards, and requests aimed at Cadence.
            if subtype and subtype != "bot_message":
                return
            if (event.get("bot_id") or subtype == "bot_message") and any(
                b.get("type") in {"section", "header", "actions", "divider", "image"}
                for b in event.get("blocks") or []
            ):
                return
            text = event.get("text") or ""
            bot_user = context.get("bot_user_id")
            if bot_user and f"<@{bot_user}>" in text:
                return  # requests to Cadence are not workspace intelligence
            uid = event.get("user") or (f"persona:{event['username']}" if event.get("username") else None)
            uname = event.get("username") or _user_name(client, event.get("user"))
            ctx = _feature_ctx(client, event.get("user"), event.get("channel"), context.get("team_id"))
            ctx.store.upsert_message(
                event["channel"], None, event["ts"],
                uid, uname,
                text,
            )
            # advance the sync cursor so the next sync_channels doesn't re-fetch
            # (and re-scan) what the live listener already processed
            last = ctx.store.last_synced_ts(event["channel"])
            if last is None or float(event["ts"]) > float(last):
                ctx.store.mark_synced(event["channel"], event["ts"])
            meta = {
                "channel_id": event["channel"], "channel_name": None, "ts": event["ts"],
                "user_id": uid, "user_name": uname,
                "permalink": None,
            }
            for hook in feature_registry.scan_hooks():
                try:
                    hook(event.get("text") or "", meta, ctx)
                except Exception as exc:  # noqa: BLE001
                    log(f"scan hook failed: {exc}")

    def _reply(body, client, blocks, text):
        channel = body["channel"]["id"]
        message = body.get("message", {})
        thread_ts = message.get("thread_ts") or message.get("ts")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, blocks=blocks, text=text)

    @app.action(re.compile(r"^book_slot_\d+$"))
    def on_book_slot(ack, body, action, client, context):
        ack()
        if _already_handled(action["action_id"], action.get("value", "")):
            _reply(body, client, block_kit.error_card("Already booked that one — see the confirmation above. :white_check_mark:"), "Already booked")
            return
        payload = json.loads(action["value"])
        topic = payload.get("topic") or "Team sync"
        title = topic[0].upper() + topic[1:] if topic else "Team sync"
        log(f"booking slot {payload['start']} for {payload['emails']}")
        try:
            event = mcp().call_tool(
                "create_event",
                {
                    "title": title,
                    "attendees": payload["emails"],
                    "start": payload["start"],
                    "end": payload["end"],
                },
                timeout=10,
            )
        except McpError as exc:
            _reply(body, client, block_kit.error_card(str(exc)), "Booking failed")
            return

        items, trace = search_context(
            client,
            query=topic,
            channel_id=body["channel"]["id"],
            team_id=context.get("team_id"),
            user_token=os.environ.get("SLACK_USER_TOKEN") or None,
        )
        names = [e.split("@")[0].title() for e in payload["emails"]]
        agenda_text = draft_agenda(topic, names, items)
        _reply(body, client, block_kit.booking_confirmation(event, agenda_text, trace.footer()), "Meeting booked")

    @app.action(re.compile(r"^assign_cover_\d+$"))
    def on_assign_cover(ack, body, action, client, context):
        ack()
        if _already_handled(action["action_id"], action.get("value", "")):
            _reply(body, client, block_kit.error_card("Already assigned — see the confirmation above. :white_check_mark:"), "Already assigned")
            return
        payload = json.loads(action["value"])
        log(f"assigning cover for {payload['event_id']} -> {payload['to_email']}")
        items, trace = search_context(
            client,
            query=payload["title"],
            channel_id=body["channel"]["id"],
            team_id=context.get("team_id"),
            user_token=os.environ.get("SLACK_USER_TOKEN") or None,
        )
        substitute_name = payload["to_email"].split("@")[0].title()
        brief = draft_handover({"title": payload["title"], "start": payload["start"]}, substitute_name, items)
        try:
            event = mcp().call_tool(
                "reassign_event",
                {
                    "event_id": payload["event_id"],
                    "from_email": ME_EMAIL,
                    "to_email": payload["to_email"],
                    "note": brief,
                },
                timeout=10,
            )
        except McpError as exc:
            _reply(body, client, block_kit.error_card(str(exc)), "Reassignment failed")
            return
        _reply(body, client, block_kit.coverage_confirmation(event, payload["to_email"], brief, trace.footer()), "Cover assigned")

    feature_actions = feature_registry.action_handlers()
    if feature_actions:
        pattern = re.compile("^(" + "|".join(re.escape(p) for p in feature_actions) + ")")

        @app.action(pattern)
        def on_feature_action(ack, body, action, client, context):
            ack()
            action_id = action["action_id"]
            if _already_handled(action_id, action.get("value", "")):
                return  # feature actions have their own status guards; stay quiet on repeats
            handler = next(
                (fn for prefix, fn in feature_actions.items() if action_id.startswith(prefix)), None
            )
            if handler is None:
                return
            try:
                payload = json.loads(action.get("value") or "{}")
            except json.JSONDecodeError:
                payload = {}
            ctx = _feature_ctx(
                client, body.get("user", {}).get("id"),
                body.get("channel", {}).get("id"), context.get("team_id"),
            )
            try:
                blocks, text = handler(payload, ctx)
            except Exception as exc:  # noqa: BLE001
                log(f"feature action {action_id} crashed: {exc}")
                blocks, text = block_kit.error_card(f"Action failed: {exc}"), "Action failed"
            _reply(body, client, blocks, text)

    @app.event("app_home_opened")
    def on_home(event, client):
        if event.get("tab") != "home":
            return
        client.views_publish(
            user_id=event["user"],
            view={
                "type": "home",
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "🎼 Cadence"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": block_kit.help_text()}},
                ],
            },
        )

    return app


def main() -> None:
    load_dotenv(ROOT / ".env")
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token or app_token.startswith("xapp-your"):
        raise RuntimeError("Set SLACK_APP_TOKEN in .env")
    app = build_app()
    mcp()  # connect eagerly so a broken MCP server fails at startup, loudly
    from .digest import start_scheduler

    start_scheduler(app.client, lambda: _feature_ctx(client=app.client), ME_EMAIL)
    log("Cadence starting (Socket Mode)…")
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
