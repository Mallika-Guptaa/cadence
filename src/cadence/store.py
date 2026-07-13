"""Persistent store for workspace intelligence: SQLite + FTS5.

Design notes (sized for very large histories):
- Message cache is an append-mostly table keyed (channel_id, ts) with an FTS5
  full-text index kept in sync by triggers. Topic search, expertise mining,
  and duplicate-discussion lookups are all single indexed SQL queries — no
  Python-side scans over message lists, so cost grows with result size, not
  history size.
- WAL journal mode + one connection per thread: Bolt handles each Slack event
  on its own worker thread, and SQLite in WAL mode gives concurrent readers
  with a single writer, which matches our write-light/read-heavy shape.
- bm25 ranking orders full-text hits; recency is a tiebreaker.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(os.environ.get("CADENCE_DB", str(ROOT / "cadence.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  channel_id   TEXT NOT NULL,
  channel_name TEXT,
  ts           TEXT NOT NULL,
  ts_num       REAL NOT NULL,
  user_id      TEXT,
  user_name    TEXT,
  text         TEXT NOT NULL,
  permalink    TEXT,
  PRIMARY KEY(channel_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_messages_ts   ON messages(ts_num);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, ts_num);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  text, content='messages', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.rowid, old.text);
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS promises(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id    TEXT,
  owner_name  TEXT,
  text        TEXT NOT NULL,
  due_ts      REAL,
  status      TEXT NOT NULL DEFAULT 'open',
  channel_id  TEXT,
  message_ts  TEXT,
  permalink   TEXT,
  created_ts  REAL NOT NULL,
  UNIQUE(channel_id, message_ts, text)
);
CREATE INDEX IF NOT EXISTS idx_promises_status_due ON promises(status, due_ts);

CREATE TABLE IF NOT EXISTS sync_state(
  channel_id TEXT PRIMARY KEY,
  last_ts    TEXT NOT NULL
);
"""


def _fts_quote(term: str) -> str:
    cleaned = re.sub(r"[^\w-]", "", term)
    return f'"{cleaned}"' if cleaned else ""


class Store:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self._path = str(path)
        self._local = threading.local()
        self._conn().executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # -- message cache --------------------------------------------------------

    def upsert_message(
        self,
        channel_id: str,
        channel_name: str | None,
        ts: str,
        user_id: str | None,
        user_name: str | None,
        text: str,
        permalink: str | None = None,
    ) -> None:
        if not text:
            return
        conn = self._conn()
        with conn:
            conn.execute(
                """INSERT INTO messages(channel_id, channel_name, ts, ts_num, user_id, user_name, text, permalink)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(channel_id, ts) DO UPDATE SET
                     text=excluded.text, user_name=excluded.user_name,
                     channel_name=excluded.channel_name,
                     permalink=COALESCE(excluded.permalink, messages.permalink)""",
                (channel_id, channel_name, ts, float(ts), user_id, user_name, text, permalink),
            )

    def search_messages(
        self,
        terms: list[str],
        limit: int = 10,
        since_ts: float | None = None,
        exclude_ts: str | None = None,
    ) -> list[sqlite3.Row]:
        """bm25-ranked full-text search; cost scales with matches, not history."""
        match = " OR ".join(q for q in (_fts_quote(t) for t in terms) if q)
        if not match:
            return []
        sql = """SELECT m.*, bm25(messages_fts) AS score
                 FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid
                 WHERE messages_fts MATCH ?"""
        params: list = [match]
        if since_ts is not None:
            sql += " AND m.ts_num >= ?"
            params.append(since_ts)
        if exclude_ts is not None:
            sql += " AND m.ts != ?"
            params.append(exclude_ts)
        sql += " ORDER BY score, m.ts_num DESC LIMIT ?"
        params.append(limit)
        return list(self._conn().execute(sql, params))

    def messages_since(self, since_ts: float, limit: int = 500) -> list[sqlite3.Row]:
        return list(
            self._conn().execute(
                "SELECT * FROM messages WHERE ts_num >= ? ORDER BY ts_num DESC LIMIT ?",
                (since_ts, limit),
            )
        )

    def expertise_for(self, terms: list[str], limit: int = 3) -> list[sqlite3.Row]:
        """Who actually talks about these terms — hits per user, one SQL query.

        The sample columns come from the user's own most recent matching row
        (window function), so the "view a sample" permalink always resolves.
        """
        match = " OR ".join(q for q in (_fts_quote(t) for t in terms) if q)
        if not match:
            return []
        return list(
            self._conn().execute(
                """WITH hits AS (
                     SELECT m.user_id, m.user_name, m.ts_num, m.channel_id, m.ts,
                            ROW_NUMBER() OVER (PARTITION BY m.user_id ORDER BY m.ts_num DESC) AS rn
                     FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid
                     WHERE messages_fts MATCH ? AND m.user_name IS NOT NULL
                   )
                   SELECT user_id, user_name, COUNT(*) AS hits,
                          MAX(ts_num) AS last_seen,
                          MAX(CASE WHEN rn = 1 THEN channel_id END) AS channel_id,
                          MAX(CASE WHEN rn = 1 THEN ts END) AS sample_ts
                   FROM hits GROUP BY user_id
                   ORDER BY hits DESC, last_seen DESC LIMIT ?""",
                (match, limit),
            )
        )

    def message_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    # -- promises --------------------------------------------------------------

    def add_promise(
        self,
        owner_id: str | None,
        owner_name: str | None,
        text: str,
        due_ts: float | None,
        channel_id: str | None,
        message_ts: str | None,
        permalink: str | None,
    ) -> int | None:
        """Insert a promise; returns id, or None if this exact one is already tracked."""
        conn = self._conn()
        try:
            with conn:
                cursor = conn.execute(
                    """INSERT INTO promises(owner_id, owner_name, text, due_ts, channel_id, message_ts, permalink, created_ts)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (owner_id, owner_name, text, due_ts, channel_id, message_ts, permalink, time.time()),
                )
                return cursor.lastrowid
        except sqlite3.IntegrityError:
            return None

    def open_promises(self, owner_id: str | None = None, overdue_before: float | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM promises WHERE status = 'open'"
        params: list = []
        if owner_id:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        if overdue_before is not None:
            sql += " AND due_ts IS NOT NULL AND due_ts < ?"
            params.append(overdue_before)
        sql += " ORDER BY due_ts IS NULL, due_ts ASC, created_ts ASC"
        return list(self._conn().execute(sql, params))

    def set_promise_status(self, promise_id: int, status: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE promises SET status = ? WHERE id = ?", (status, promise_id))

    def get_promise(self, promise_id: int) -> sqlite3.Row | None:
        return self._conn().execute("SELECT * FROM promises WHERE id = ?", (promise_id,)).fetchone()

    # -- sync bookkeeping --------------------------------------------------------

    def last_synced_ts(self, channel_id: str) -> str | None:
        row = self._conn().execute(
            "SELECT last_ts FROM sync_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["last_ts"] if row else None

    def mark_synced(self, channel_id: str, last_ts: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """INSERT INTO sync_state(channel_id, last_ts) VALUES(?,?)
                   ON CONFLICT(channel_id) DO UPDATE SET last_ts=excluded.last_ts""",
                (channel_id, last_ts),
            )
