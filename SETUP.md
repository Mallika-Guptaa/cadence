# Cadence — Setup & Reproduce (fresh laptop)

Everything needed to run Cadence from scratch on another machine. No prior state
is assumed. The bundle intentionally excludes `.venv/` (rebuild it), `.env`
(your secrets), and runtime artifacts (database, `.ics`, tasks) — all recreated
below.

---

## 0. Prerequisites

- **Python 3.11 or newer** (`python3 --version`). On macOS the system Python may
  be 3.9 — install 3.11+ via Homebrew (`brew install python@3.12`) or python.org.
- **Internet access** (Slack Socket Mode + optional LLM).
- A **Slack workspace** you can create an app in (a free developer sandbox is fine:
  https://api.slack.com/developer-program).
- Optional: an **OpenAI** or **Anthropic** API key for LLM-drafted text. Cadence
  runs fully without one (deterministic fallbacks).

---

## 1. Unzip & create the environment

```bash
unzip cadence-submission-*.zip
cd cadence

python3.12 -m venv .venv          # use a 3.11+ interpreter
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` pulls: `slack-bolt`, `slack-sdk`, `mcp`, `openai`,
`anthropic`, `pydantic`, `python-dotenv`.

---

## 2. Generate demo data & verify (no Slack needed)

```bash
python scripts/gen_calendars.py   # demo calendars for the upcoming week
./scripts/verify.sh               # 140 unit tests + 3 integration suites
```

Expect `✅ ALL VERIFICATION PASSED`. This proves every feature works end-to-end
before you touch Slack. You can also run the RCA-free CLI checks individually —
see the commands inside `verify.sh`.

---

## 3. Create the Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**.
2. Pick your workspace, paste the contents of **`manifest.json`**, create.
3. **Install to Workspace** (approve the scopes).

The manifest already declares Socket Mode, the Assistant surface, the `/schedule`
command, all bot/user scopes, and the event subscriptions — no manual config.

---

## 4. Add tokens to `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in (all from the app's settings pages):

| Variable | Where to get it | Required |
|---|---|---|
| `SLACK_BOT_TOKEN` (`xoxb-…`) | OAuth & Permissions → Bot User OAuth Token | ✅ |
| `SLACK_APP_TOKEN` (`xapp-…`) | Basic Information → App-Level Tokens → create with `connections:write` | ✅ |
| `SLACK_USER_TOKEN` (`xoxp-…`) | OAuth & Permissions → User OAuth Token | recommended (enables Real-Time Search + human-authored seeding) |
| `OPENAI_API_KEY` **or** `ANTHROPIC_API_KEY` | your provider | optional (LLM polish) |

Leave the `CADENCE_*` tuning knobs at their defaults. `.env` is git-ignored —
never commit it.

---

## 5. Seed the demo workspace

Have the bot join the channels, then seed content so the features have material:

```bash
# invite the bot in Slack first, or let it self-join public channels
python scripts/seed_channel.py        # topic context + promises + ship notes in #general
python scripts/seed_multiuser.py      # 10 personas across 8 #cadence-* channels (threaded)
python scripts/seed_mentions.py       # a few messages that @-mention you (for Catch-Me-Up)
```

`seed_multiuser.py` needs the bot scopes `channels:manage` + `chat:write.customize`
(already in the manifest). If it errors with `missing_scope`, reinstall the app.

---

## 6. Run it

```bash
python app.py            # → "⚡️ Bolt app is running!"
```

Keep this terminal open — Socket Mode holds the connection here. **Run only ONE
instance** (multiple copies split events and cause inconsistent replies).

In Slack, try:
- Open the **Cadence** assistant pane → click a suggested prompt
- `@Cadence find 45 minutes for me, Priya and Marco this week` → click a slot
- `@Cadence I'm on leave Thursday and Friday — find cover for my meetings`
- `@Cadence show open promises` · `what did I miss in the last 2 days?`
- `@Cadence who knows about postgres partitioning?` · `compile release notes`
- `@Cadence was the redis eviction policy discussed before?`

Every calendar/task/changelog side effect prints in the terminal as
`[cadence-mcp] …` and writes real files under `mcp_server/` (open the `.ics` in
your calendar app).

---

## 7. Before recording a demo

```bash
./scripts/demo_reset.sh   # fresh calendars, clean store, no leftover artifacts
python app.py
```

Set `CADENCE_DIGEST_ON_START=1` in `.env` to have the morning digest post the
moment the app starts (a nice opening shot). Set it to `0` to avoid digests on
every restart. Full shot list: `docs/DEMO_SCRIPT.md`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `TypeError: unable to evaluate 'str | None'` on import | You're on Python 3.9. Recreate the venv with 3.11+. |
| `Set SLACK_APP_TOKEN` at startup | `.env` missing the `xapp-…` token. |
| App runs, Slack silent | Bot not invited to the channel; or a second instance is running — stop all but one. |
| Replies look "old" / inconsistent | Another copy of `app.py` is running (another terminal/laptop). Stop it; keep one. |
| Déjà Vu says "new ground" right after seeding | Set `CADENCE_DEJAVU_MIN_AGE_SECONDS=0` for same-day-seeded demos. |
| `missing_scope` when seeding personas | Add `channels:manage` + `chat:write.customize`, reinstall the app. |

---

## What's in the box

```
cadence/
  app.py                     entry point (Socket Mode)
  manifest.json              Slack app manifest (paste to create the app)
  requirements.txt           dependencies
  .env.example               token template (copy to .env)
  README.md                  overview + architecture
  SETUP.md                   this file
  KNOWN_LIMITS.md            honest edge cases (from the adversarial review)
  IDEAS.md                   the 22-idea catalog we brainstormed from
  src/cadence/               the agent: routing, features, store, RTS, MCP client, digest
  mcp_server/                the MCP server + JSON calendar/project fixtures
  scripts/                   gen_calendars, seed_*, verify.sh, demo_reset.sh, load_store
  tests/                     140 unit tests + 3 integration suites
  docs/                      DEMO_SCRIPT.md, DEVPOST.md, architecture.png
```

Deliberately **not** included: `.venv/` (rebuild via step 1), `.env` (your
secrets), `cadence.db` and `mcp_server/{events,tasks,changelog}/*` (regenerated
at runtime), `.git/`, `__pycache__/`.
