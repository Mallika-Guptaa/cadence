"""End-to-end check of all six productivity features through handle_request.

    PYTHONPATH=src .venv/bin/python tests/integration/test_features_pipeline.py

Real MCP server subprocess for status / release notes / task filing; seeded
temp store instead of Slack (client=None makes sync a no-op). Cleans up its
artifacts afterwards.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cadence import features as registry  # noqa: E402
from cadence import slack_app  # noqa: E402
from cadence.store import Store  # noqa: E402


class Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)

    @property
    def text(self):
        return json.dumps(self.calls[-1].get("blocks", [])) + self.calls[-1].get("text", "")


def seed(store: Store) -> None:
    now = time.time()

    def msg(offset_h, user_id, user_name, text, channel=("C1", "#general")):
        ts = f"{now - offset_h * 3600:.4f}"
        store.upsert_message(channel[0], channel[1], ts, user_id, user_name, text)
        return ts, channel

    # promises — run through the real scan hooks, like sync would
    ctx = slack_app._feature_ctx()
    for offset, uid, uname, text in [
        (5, "U1", "Priya", "I'll send the updated pricing deck by Friday."),
        (4, "U2", "Marco", "We'll fix the checkout redirect bug next sprint."),
    ]:
        ts, channel = msg(offset, uid, uname, text)
        meta = {"channel_id": channel[0], "channel_name": channel[1], "ts": ts,
                "user_id": uid, "user_name": uname, "permalink": None}
        for hook in registry.scan_hooks():
            hook(text, meta, ctx)

    # catch-up + expertise + release notes material
    msg(6, "U1", "Priya", "Kubernetes ingress TLS config is documented in the values file now")
    msg(7, "U1", "Priya", "More kubernetes ingress notes: cert-manager handles rotation")
    msg(8, "U2", "Marco", "Shipped the new checkout flow to 10% of traffic")
    msg(9, "U2", "Marco", "Fixed the double-charge bug in payments retries")
    msg(10, "U3", "Dana", "Phoenix cutover rehearsal notes posted, load test pending")
    # déjà vu needs prior discussion older than a day
    store.upsert_message("C1", "#general", f"{now - 3 * 86400:.4f}", "U3", "Dana",
                         "We discussed SSO setup for enterprise customers - decision was SAML for tier-1")


def main() -> None:
    slack_app._store = Store(tempfile.mktemp(suffix=".db"))
    seed(slack_app._store)

    def ask(text: str) -> Capture:
        say = Capture()
        slack_app.handle_request(text, say)
        assert say.calls, f"no reply for {text!r}"
        return say

    out = ask("show open promises")
    assert "pricing deck" in out.text and "promise_done" in out.text, out.text[:400]
    print("promises OK: digest lists commitments with buttons")

    # exercise the File-task button through the real MCP server
    handler = registry.action_handlers()["promise_task"]
    promise = slack_app._store.open_promises()[0]
    blocks, _ = handler(
        {"id": promise["id"], "title": promise["text"], "owner": promise["owner_name"] or "", "due": ""},
        slack_app._feature_ctx(),
    )
    assert "TASK-" in json.dumps(blocks), blocks
    print("promises OK: File task -> real MCP create_task")

    out = ask("what did I miss in the last 2 days?")
    assert "#general" in out.text, out.text[:400]
    print("catch-me-up OK")

    out = ask("what's the status of project phoenix?")
    assert "Phoenix" in out.text and ("blocked" in out.text.lower() or "risk" in out.text.lower()), out.text[:400]
    print("status OK: real MCP get_project + health card")

    out = ask("who knows about kubernetes ingress?")
    assert "Priya" in out.text, out.text[:400]
    print("experts OK: Priya identified")

    out = ask("compile release notes")
    assert "checkout" in out.text.lower(), out.text[:400]
    changelog = ROOT / "mcp_server" / "changelog" / "CHANGELOG.md"
    assert changelog.exists(), "changelog not published"
    print("release notes OK: published to", changelog.name)

    out = ask("was SSO setup discussed before?")
    assert "SSO" in out.text or "SAML" in out.text, out.text[:400]
    print("deja vu OK: prior discussion surfaced")

    print("FEATURES PIPELINE TEST PASSED")


if __name__ == "__main__":
    try:
        main()
    finally:
        if slack_app._mcp is not None:
            slack_app._mcp.close()
        for stray in (ROOT / "mcp_server" / "tasks").glob("TASK-*.json"):
            stray.unlink()
        changelog = ROOT / "mcp_server" / "changelog" / "CHANGELOG.md"
        if changelog.exists():
            changelog.unlink()
        print("artifacts cleaned")
