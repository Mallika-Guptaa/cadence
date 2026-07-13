#!/bin/bash
# One-command reset before recording: fresh fixtures, clean store, no leftover
# artifacts. Run from the cadence/ directory, then restart `python app.py`.
set -e
cd "$(dirname "$0")/.."

pkill -f "\.venv/bin/python app\.py" 2>/dev/null && echo "stopped running app" || echo "no app running"
./.venv/bin/python scripts/gen_calendars.py
rm -f cadence.db cadence.db-wal cadence.db-shm
rm -f mcp_server/events/EVT-* mcp_server/events/LVE-* mcp_server/tasks/TASK-*.json
rm -f mcp_server/changelog/CHANGELOG.md
echo "reset complete — start the app:  ./.venv/bin/python app.py"
echo "(CADENCE_DIGEST_ON_START=1 will post the digest right after startup)"
