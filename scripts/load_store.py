"""Scale-test loader: inject a big synthetic workspace history into the store.

    CADENCE_DB=scale_test.db python scripts/load_store.py [messages=5000] [days=90]

Generates realistic multi-persona, multi-channel chatter (topics, promises,
ship notes, prior discussions), loads it through the same Store API the app
uses, runs the promise scan hooks, then benchmarks the queries every feature
depends on. Use a separate CADENCE_DB so the live demo store stays clean.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cadence.store import DEFAULT_DB, Store  # noqa: E402

PERSONAS = [
    ("U100", "Priya Sharma"), ("U101", "Marco Rossi"), ("U102", "Dana Kim"),
    ("U103", "Alex Chen"), ("U104", "Sam Patel"), ("U105", "Yuki Tanaka"),
    ("U106", "Omar Farouk"), ("U107", "Lena Novak"),
]
CHANNELS = [
    ("C200", "#general"), ("C201", "#engineering"), ("C202", "#support"),
    ("C203", "#release-notes"), ("C204", "#payments-team"), ("C205", "#infra"),
    ("C206", "#design"), ("C207", "#data"), ("C208", "#security"),
    ("C209", "#onboarding"), ("C210", "#random"), ("C211", "#incidents"),
]
TOPICS = [
    "kubernetes ingress TLS rotation", "payments migration cutover",
    "SSO SAML for enterprise tiers", "dashboard query engine",
    "checkout redirect flow", "rate limiting for the public API",
    "postgres partitioning strategy", "mobile deep links",
    "GDPR data retention jobs", "feature flag cleanup",
]
TEMPLATES = [
    "Quick update on {t}: made progress today, review is pending.",
    "Anyone have context on {t}? Hitting an edge case in staging.",
    "The {t} work is unblocked now, thanks for the review.",
    "Wrote up notes on {t} in the wiki, feedback welcome.",
    "Heads up: {t} changes roll out behind a flag first.",
    "Shipped the first part of {t} to production today.",
    "Fixed the flaky behavior in {t}, root cause was a stale cache.",
    "I'll send the summary doc for {t} by friday.",
    "We'll fix the remaining issues in {t} next sprint.",
    "We discussed {t} at length - decision: keep the current approach for Q3.",
]


def main() -> None:
    n_messages = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    rng = random.Random(42)  # reproducible

    print(f"loading {n_messages} messages / {len(PERSONAS)} people / {len(CHANNELS)} channels / {days} days")
    print(f"database: {DEFAULT_DB}")
    store = Store()

    now = time.time()
    t0 = time.perf_counter()
    for i in range(n_messages):
        uid, uname = rng.choice(PERSONAS)
        cid, cname = rng.choice(CHANNELS)
        topic = rng.choice(TOPICS)
        text = rng.choice(TEMPLATES).format(t=topic)
        ts = f"{now - rng.uniform(0, days * 86400):.6f}"
        store.upsert_message(cid, cname, ts, uid, uname, text)
    load_s = time.perf_counter() - t0
    print(f"loaded in {load_s:.1f}s ({n_messages / load_s:.0f} msg/s) — store now has {store.message_count()} messages")

    # run the real promise scanner over a recent slice, as sync would
    from cadence.features import FeatureContext, scan_hooks
    ctx = FeatureContext(store=store, mcp=lambda: None)
    scanned = 0
    for row in store.messages_since(now - 14 * 86400, limit=2000):
        meta = {"channel_id": row["channel_id"], "channel_name": row["channel_name"], "ts": row["ts"],
                "user_id": row["user_id"], "user_name": row["user_name"], "permalink": None}
        for hook in scan_hooks():
            hook(row["text"], meta, ctx)
        scanned += 1
    print(f"promise scan over {scanned} recent messages -> {len(store.open_promises())} open promises")

    # benchmark the queries every feature runs
    def bench(label, fn, runs=20):
        start = time.perf_counter()
        result = None
        for _ in range(runs):
            result = fn()
        ms = (time.perf_counter() - start) / runs * 1000
        size = len(result) if hasattr(result, "__len__") else result
        print(f"  {label:<42s} {ms:7.2f} ms/query   ({size} rows)")

    print("query benchmarks (mean over 20 runs):")
    bench("FTS search: 'payments migration cutover'", lambda: store.search_messages(["payments", "migration", "cutover"], limit=25))
    bench("expertise: 'kubernetes ingress'", lambda: store.expertise_for(["kubernetes", "ingress"], limit=3))
    bench("deja vu pool: 'SSO SAML enterprise'", lambda: store.search_messages(["sso", "saml", "enterprise"], limit=25))
    bench("catch-up: messages_since 2 days", lambda: store.messages_since(now - 2 * 86400, limit=400))
    bench("promises: open digest", lambda: store.open_promises())
    print("SCALE LOAD COMPLETE")


if __name__ == "__main__":
    main()
