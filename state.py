"""Durable SQLite inbox for Relay WebSocket events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        self._db.execute("PRAGMA foreign_keys=ON")
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
            CREATE TABLE IF NOT EXISTS snapshot_chats (
              chat_id TEXT PRIMARY KEY,
              chat_json TEXT NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_messages (
              message_id TEXT PRIMARY KEY,
              chat_id TEXT NOT NULL REFERENCES snapshot_chats(chat_id)
                ON DELETE CASCADE,
              message_json TEXT NOT NULL
            )
            """
        )
        self._db.execute(
            """
            CREATE INDEX IF NOT EXISTS snapshot_messages_chat
            ON snapshot_messages(chat_id, message_id)
            """
        )
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS full_sync_state (
              singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
              through_sequence TEXT NOT NULL,
              snapshot_sha256 TEXT NOT NULL,
              chat_count INTEGER NOT NULL,
              message_count INTEGER NOT NULL,
              committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        """Commit a delivery and deduplicated event in one transaction.

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

    def replace_full_snapshot(
        self,
        through_sequence: str,
        snapshot: List[Dict[str, Any]],
    ) -> str:
        """Atomically replace the REST snapshot and its WebSocket checkpoint.

        Chats and Messages are sorted by stable ids before hashing and writing,
        so equivalent paginated responses produce the same snapshot digest.
        The caller may send ``full_sync_complete`` only after this returns.
        """

        if (
            not through_sequence.isdigit()
            or (
                len(through_sequence) > 1
                and through_sequence.startswith("0")
            )
        ):
            raise ValueError(
                "Relay FULL sync checkpoint must be a decimal string"
            )
        chats: Dict[str, Dict[str, Any]] = {}
        messages: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        for entry in snapshot:
            if not isinstance(entry, dict) or set(entry) != {"chat", "messages"}:
                raise ValueError("Relay FULL sync snapshot entry is invalid")
            chat = entry.get("chat")
            page_messages = entry.get("messages")
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            if (
                not isinstance(chat, dict)
                or not isinstance(chat_id, str)
                or not chat_id
                or not isinstance(page_messages, list)
            ):
                raise ValueError("Relay FULL sync Chat is invalid")
            if chat_id in chats:
                raise ValueError(
                    f"Relay FULL sync contains duplicate Chat {chat_id}"
                )
            chats[chat_id] = chat
            for message in page_messages:
                message_id = (
                    message.get("id") if isinstance(message, dict) else None
                )
                if (
                    not isinstance(message, dict)
                    or not isinstance(message_id, str)
                    or not message_id
                    or message.get("chat_id") != chat_id
                ):
                    raise ValueError(
                        f"Relay FULL sync contains an invalid Message "
                        f"for Chat {chat_id}"
                    )
                if message_id in messages:
                    raise ValueError(
                        f"Relay FULL sync contains duplicate Message "
                        f"{message_id}"
                    )
                messages[message_id] = (chat_id, message)

        canonical_snapshot = [
            {
                "chat": chats[chat_id],
                "messages": [
                    messages[message_id][1]
                    for message_id in sorted(messages)
                    if messages[message_id][0] == chat_id
                ],
            }
            for chat_id in sorted(chats)
        ]
        try:
            canonical = json.dumps(
                canonical_snapshot,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Relay FULL sync snapshot is not canonical JSON"
            ) from exc
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        db = self._connection()
        with db:
            db.execute("DELETE FROM snapshot_messages")
            db.execute("DELETE FROM snapshot_chats")
            for entry in canonical_snapshot:
                chat = entry["chat"]
                chat_id = str(chat["id"])
                db.execute(
                    """
                    INSERT INTO snapshot_chats(chat_id,chat_json)
                    VALUES(?,?)
                    """,
                    (
                        chat_id,
                        json.dumps(
                            chat,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
                for message in entry["messages"]:
                    db.execute(
                        """
                        INSERT INTO snapshot_messages(
                          message_id,chat_id,message_json
                        ) VALUES(?,?,?)
                        """,
                        (
                            str(message["id"]),
                            chat_id,
                            json.dumps(
                                message,
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        ),
                    )
            db.execute(
                """
                INSERT INTO full_sync_state(
                  singleton,through_sequence,snapshot_sha256,
                  chat_count,message_count,committed_at
                ) VALUES(1,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(singleton) DO UPDATE SET
                  through_sequence=excluded.through_sequence,
                  snapshot_sha256=excluded.snapshot_sha256,
                  chat_count=excluded.chat_count,
                  message_count=excluded.message_count,
                  committed_at=CURRENT_TIMESTAMP
                """,
                (
                    through_sequence,
                    digest,
                    len(chats),
                    len(messages),
                ),
            )
        return digest

    def load_full_snapshot(self) -> List[Dict[str, Any]]:
        """Read the last complete snapshot in deterministic order."""

        db = self._connection()
        result: List[Dict[str, Any]] = []
        for chat_id, raw_chat in db.execute(
            """
            SELECT chat_id,chat_json
            FROM snapshot_chats
            ORDER BY chat_id
            """
        ):
            rows = db.execute(
                """
                SELECT message_json
                FROM snapshot_messages
                WHERE chat_id=?
                ORDER BY message_id
                """,
                (str(chat_id),),
            ).fetchall()
            result.append({
                "chat": json.loads(str(raw_chat)),
                "messages": [json.loads(str(row[0])) for row in rows],
            })
        return result

    def full_sync_state(self) -> Optional[Dict[str, Any]]:
        row = self._connection().execute(
            """
            SELECT through_sequence,snapshot_sha256,chat_count,message_count
            FROM full_sync_state
            WHERE singleton=1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "through_sequence": str(row[0]),
            "snapshot_sha256": str(row[1]),
            "chat_count": int(row[2]),
            "message_count": int(row[3]),
        }

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
