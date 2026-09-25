"""sqlite storage: seen postings (dedupe), bot settings, and heartbeat counters."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    key          TEXT PRIMARY KEY,      -- e.g. "hh:12345"
    source       TEXT NOT NULL,         -- site it was first seen on
    external_id  TEXT NOT NULL,
    title        TEXT,
    url          TEXT,
    status       TEXT NOT NULL,         -- 'scored' | 'skipped_initial' | 'refused'
    score        INTEGER,
    reason       TEXT,
    message      TEXT,                  -- rendered notification, kept for resending
    notified     INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name   TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);
"""

COUNTER_NAMES = ("checks", "new_postings", "notifications", "errors")


@dataclass
class PendingNotification:
    key: str
    message: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- dedupe -----------------------------------------------------------

    def seen_keys(self, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        placeholders = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT key FROM postings WHERE key IN ({placeholders})", keys
        ).fetchall()
        return {row[0] for row in rows}

    def has_source_history(self, source: str) -> bool:
        return self.get_setting(f"initialized:{source}") is not None

    def mark_source_initialized(self, source: str) -> None:
        self.set_setting(f"initialized:{source}", _now())

    def record(
        self,
        *,
        key: str,
        source: str,
        external_id: str,
        title: str,
        url: str,
        status: str,
        score: int | None = None,
        reason: str | None = None,
        message: str | None = None,
    ) -> bool:
        """Insert a posting if it isn't already stored. Returns True if inserted."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO postings
               (key, source, external_id, title, url, status, score, reason, message, first_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, source, external_id, title, url, status, score, reason, message, _now()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def pending_notifications(self, threshold: int) -> list[PendingNotification]:
        rows = self.conn.execute(
            """SELECT key, message FROM postings
               WHERE status = 'scored' AND notified = 0 AND score >= ? AND message IS NOT NULL
               ORDER BY first_seen""",
            (threshold,),
        ).fetchall()
        return [PendingNotification(key=r[0], message=r[1]) for r in rows]

    def mark_notified(self, key: str) -> None:
        self.conn.execute("UPDATE postings SET notified = 1 WHERE key = ?", (key,))
        self.conn.commit()

    # --- settings ---------------------------------------------------------

    def get_setting(self, name: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def set_setting(self, name: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )
        self.conn.commit()

    # --- heartbeat counters -----------------------------------------------

    def bump(self, name: str, amount: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value",
            (name, amount),
        )
        self.conn.commit()

    def counters(self) -> dict[str, int]:
        values = dict(self.conn.execute("SELECT name, value FROM counters").fetchall())
        return {name: int(values.get(name, 0)) for name in COUNTER_NAMES}

    def reset_counters(self) -> None:
        self.conn.execute("DELETE FROM counters")
        self.conn.commit()
