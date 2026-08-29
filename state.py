"""Durable SQLite inbox for Relay WebSocket events."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_STATE_DIR = "~/.hermes/relay"
STATE_FILENAME = "inbox.sqlite3"


class RelayInbox:
    """Store an event before its cumulative WebSocket ACK is sent.

    ``event_id`` is the processing dedupe key. ``sequence`` is retained
    separately because the same event can be replayed at a sequence that still
    needs acknowledging.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: Optional[sqlite3.Connection] = None

    def open(self) -> "RelayInbox":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              event_id TEXT PRIMARY KEY,
              envelope_json TEXT NOT NULL,
              status TEXT NOT NULL,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
              sequence TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # A previous gateway process cannot still own its in-memory Hermes
        # task. Requeue rows that were handed to that process before it exited.
        self._db.execute(
            """
            UPDATE events
            SET status = 'pending', updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('processing', 'dispatched')
            """
        )
        self._db.commit()
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        return self

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Relay inbox is not open")
        return self._db

    def accept(self, sequence: str, event: Dict[str, Any]) -> bool:
        """Commit a delivery and deduplicated event in one FULL-sync transaction.

        Returns true only when ``event_id`` created new pending work. The caller
        may ACK ``sequence`` after this method returns.
        """

        if (
            not sequence.isdigit()
            or (len(sequence) > 1 and sequence.startswith("0"))
        ):
            raise ValueError("Relay WebSocket sequence must be a decimal string")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Relay event_id is required")
        payload = json.dumps(event, separators=(",", ":"), sort_keys=True)
        db = self._connection()
        with db:
            delivered = db.execute(
                "SELECT event_id FROM deliveries WHERE sequence = ?", (sequence,)
            ).fetchone()
            if delivered is not None and str(delivered[0]) != event_id:
                raise ValueError(
                    "Relay WebSocket sequence was replayed with a different event_id"
                )
            existed = db.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            db.execute(
                """
                INSERT INTO events (event_id, envelope_json, status)
                VALUES (?, ?, 'pending')
                ON CONFLICT(event_id) DO NOTHING
                """,
                (event_id, payload),
            )
            db.execute(
                """
                INSERT INTO deliveries (sequence, event_id)
                VALUES (?, ?)
                ON CONFLICT(sequence) DO NOTHING
                """,
                (sequence, event_id),
            )
        return existed is None

    def next_pending(self) -> Optional[Tuple[str, Dict[str, Any]]]:
        db = self._connection()
        row = db.execute(
            """
            SELECT event_id, envelope_json
            FROM events
            WHERE status = 'pending'
            ORDER BY rowid
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        event_id, raw = str(row[0]), str(row[1])
        with db:
            db.execute(
                """
                UPDATE events
                SET status = 'processing', attempt_count = attempt_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE event_id = ?
                """,
                (event_id,),
            )
        return event_id, json.loads(raw)

    def mark_dispatched(self, event_id: str) -> None:
        """Record that Hermes owns the event in an in-memory processing task."""

        db = self._connection()
        with db:
            db.execute(
                """
                UPDATE events
                SET status = 'dispatched', updated_at = CURRENT_TIMESTAMP
                WHERE event_id = ?
                """,
                (event_id,),
            )

    def complete(self, event_id: str, *, ignored: bool = False) -> None:
        db = self._connection()
        with db:
            db.execute(
                """
                UPDATE events
                SET status = ?, last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE event_id = ?
                """,
                ("ignored" if ignored else "completed", event_id),
            )

    def retry(self, event_id: str, error: str) -> None:
        db = self._connection()
        with db:
            db.execute(
                """
                UPDATE events
                SET status = 'pending', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE event_id = ?
                """,
                (error[:500], event_id),
            )

    def status(self, event_id: str) -> Optional[str]:
        row = self._connection().execute(
            "SELECT status FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        return str(row[0]) if row else None

    def event_count(self) -> int:
        row = self._connection().execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row else 0


def default_state_path() -> Path:
    return Path(DEFAULT_STATE_DIR).expanduser() / STATE_FILENAME
