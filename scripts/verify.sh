#!/bin/bash
# Full verification: unit tests + all integration suites + MCP round-trip.
# Exercises every feature end-to-end (no Slack tokens needed). Run from cadence/.
#
#   ./scripts/verify.sh
set -e
cd "$(dirname "$0")/.."
PY=./.venv/bin/python
export PYTHONPATH=src

echo "━━━ 1/4  unit tests (all features) ━━━"
$PY -m unittest discover -s tests

echo ""
echo "━━━ 2/4  MCP round-trip (stdio server, 10 tools) ━━━"
$PY tests/integration/test_mcp_roundtrip.py

echo ""
echo "━━━ 3/4  scheduling flows (meeting booking + leave coverage) ━━━"
$PY tests/integration/test_pipeline.py

echo ""
echo "━━━ 4/4  productivity flows (promises, catch-up, status, experts, release notes, déjà vu) ━━━"
$PY tests/integration/test_features_pipeline.py

echo ""
echo "✅ ALL VERIFICATION PASSED — every feature exercised end-to-end."
