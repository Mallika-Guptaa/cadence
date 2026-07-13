# Known limits

Findings from an adversarial 6-lens review (concurrency, scale, time math, Slack API
contracts, failure modes, output quality) — 51 reviewer agents, 43 confirmed findings.
The demo-impacting ones were fixed; these remain by design or as accepted trade-offs.

## Operational

- **MCP subprocess does not reconnect.** If the calendar MCP server dies, calendar
  features return error cards until the app restarts (`python app.py`). The stdio
  session is held by a background thread; a timed-out call is abandoned, not
  cancelled, so a very slow tool call can still complete server-side afterwards.
- **Env knobs are read at startup.** `.env` is loaded before module import (app.py),
  but changes to `SCHEDULER_TZ` / `CADENCE_*` require a restart.
- **Idempotency guard is in-memory.** Double-click protection for buttons resets on
  restart; a click from before a restart could be re-processed (promise actions also
  have DB status guards, so those stay safe).

## Scale (sized for a sandbox / small workspace)

- **Channel sync caps at 20 member channels per pass and 200 messages per channel per
  fetch** (no cursor pagination). History older than the first 200 messages in a
  channel is never indexed. Incremental cursors make repeat syncs cheap.
- **Catch-Me-Up and Release Notes read the newest 400 cached messages** in the window;
  older activity in very busy workspaces is silently outside the digest.
- **No Slack rate-limit backoff** — the caps above keep call volume inside Tier-3
  budgets for demo-scale workspaces, but a huge workspace could still get 429s.

## Semantics (by design, worth knowing)

- **"This week" starts tomorrow** — asking on Friday proposes next week's business
  days; today is never offered (no risk of proposing a slot that just passed).
- **Déjà Vu "prior discussion"** defaults to >24h old; `CADENCE_DEJAVU_MIN_AGE_SECONDS=0`
  exists for same-day-seeded demos (set in `.env.example`).
- **Promise detection is a heuristic** (first-person future phrases, questions
  excluded). It catches the common shapes, not sarcasm or indirect commitments; the
  Done button and UNIQUE dedupe keep false positives cheap.
- **Expertise = who discusses a topic most** in indexed channels — a strong proxy,
  not an org chart. The card says exactly that in its footer.
- **Leave with non-contiguous days** ("Monday and Friday"): coverage targets only
  meetings on the named days; the calendar leave block spans the whole range.
  Coverage is leave-aware — colleagues who are themselves OOO are skipped and shown
  as such, so a substitute is never someone who's also out.
- **Slack markup flattening is heuristic** — exotic tokens inside messages render as
  plain text in cards rather than perfectly resolved names.

## Before a demo

1. Re-run `python scripts/gen_calendars.py` so fixtures sit in the coming week.
2. Seed with a user token (`SLACK_USER_TOKEN`) so promises/expertise attribute to a person.
3. Keep `CADENCE_DEJAVU_MIN_AGE_SECONDS=0` if the channel was seeded today.
4. The "on leave Thursday and Friday" prompt assumes the demo happens Mon–Wed of the
   fixture week; on other days adjust the weekday names in the prompt.
