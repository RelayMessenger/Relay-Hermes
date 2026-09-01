"""Durable SQLite inbox for Relay WebSocket events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .relay_api import normalize_base_url

DEFAULT_STATE_DIR = "~/.hermes/relay"
STATE_FILENAME = "inbox.sqlite3"
STATE_BINDING_FILENAME = ".relay-account.json"
STATE_BINDING_SCHEMA = "relay-hermes-state-account/v1"
STATE_DIRECTORY_MODE = 0o700
STATE_FILE_MODE = 0o600


class RelayStateBindingError(RuntimeError):
    """The configured Relay account does not own this durable state."""


def _account_fingerprint(api_origin: str, token_fingerprint: str) -> str:
    canonical = json.dumps(
        {
            "api_origin": api_origin,
            "token_fingerprint": token_fingerprint,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(
        b"relay-hermes-account-v1\0" + canonical.encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class RelayStateBinding:
    """Non-secret, stable identity for one token at one normalized API origin."""

    api_origin: str
    token_fingerprint: str
    account_fingerprint: str

    @classmethod
    def for_account(
        cls,
        api_origin: str,
        token: str,
    ) -> "RelayStateBinding":
        origin = (api_origin or "").strip()
        credential = (token or "").strip()
        if not origin:
            raise ValueError("relay: normalized API origin is required for state")
        origin = normalize_base_url(origin)
        if not credential:
            raise ValueError("relay: Agent Token is required for state")
        token_digest = hashlib.sha256(
            b"relay-hermes-token-v1\0" + credential.encode("utf-8")
        ).hexdigest()
        token_fingerprint = f"sha256:{token_digest}"
        return cls(
            api_origin=origin,
            token_fingerprint=token_fingerprint,
            account_fingerprint=_account_fingerprint(
                origin,
                token_fingerprint,
            ),
        )

    @classmethod
    def from_record(
        cls,
        record: Mapping[str, Any],
    ) -> "RelayStateBinding":
        expected_keys = {
            "schema",
            "api_origin",
            "token_fingerprint",
            "account_fingerprint",
        }
        if set(record) != expected_keys or record.get("schema") != STATE_BINDING_SCHEMA:
            raise RelayStateBindingError("Relay state account binding is malformed")
        api_origin = record.get("api_origin")
        token_fingerprint = record.get("token_fingerprint")
        account_fingerprint = record.get("account_fingerprint")
        if (
            not isinstance(api_origin, str)
            or not api_origin
            or not isinstance(token_fingerprint, str)
            or not token_fingerprint.startswith("sha256:")
            or len(token_fingerprint) != 71
            or not isinstance(account_fingerprint, str)
            or not account_fingerprint.startswith("sha256:")
            or len(account_fingerprint) != 71
            or not hmac.compare_digest(
                account_fingerprint,
                _account_fingerprint(api_origin, token_fingerprint),
            )
        ):
            raise RelayStateBindingError("Relay state account binding is malformed")
        return cls(
            api_origin=api_origin,
            token_fingerprint=token_fingerprint,
            account_fingerprint=account_fingerprint,
        )

    def record(self) -> Dict[str, str]:
        return {
            "schema": STATE_BINDING_SCHEMA,
            "api_origin": self.api_origin,
            "token_fingerprint": self.token_fingerprint,
            "account_fingerprint": self.account_fingerprint,
        }

    def matches(self, other: "RelayStateBinding") -> bool:
        return (
            self.api_origin == other.api_origin
            and hmac.compare_digest(
                self.token_fingerprint,
                other.token_fingerprint,
            )
            and hmac.compare_digest(
                self.account_fingerprint,
                other.account_fingerprint,
            )
        )


class RelayInbox:
    """Store an event before its cumulative WebSocket ACK is sent.

    ``event_id`` is the processing dedupe key. ``sequence`` is retained
    separately because the same event can be replayed at a sequence that still
    needs acknowledging.
    """

    def __init__(
        self,
        path: Path,
        *,
        binding: Optional[RelayStateBinding],
    ) -> None:
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.binding_path = self.path.parent / STATE_BINDING_FILENAME
        self._binding = binding
        self._db: Optional[sqlite3.Connection] = None
        self._directory_fd: Optional[int] = None

    def open(self) -> "RelayInbox":
        if self._db is not None:
            self._assert_bound()
            return self
        if self._binding is None:
            raise RelayStateBindingError(
                "Relay state requires RELAY_AGENT_TOKEN account binding"
            )
        self.path.parent.mkdir(
            mode=STATE_DIRECTORY_MODE,
            parents=True,
            exist_ok=True,
        )
        self._open_state_directory()
        db: Optional[sqlite3.Connection] = None
        try:
            directory_binding = self._read_directory_binding()
            if (
                directory_binding is not None
                and not self._binding.matches(directory_binding)
            ):
                self._raise_mismatch()

            self._assert_state_directory()
            db = sqlite3.connect(self.path)
            self._assert_state_directory()
            self._chmod_state_file(STATE_FILENAME, STATE_FILE_MODE)
            db.execute("PRAGMA foreign_keys=ON")
            database_binding, database_objects = self._read_database_binding(db)
            if (
                database_binding is not None
                and not self._binding.matches(database_binding)
            ):
                self._raise_mismatch()
            if database_binding is None and database_objects:
                raise RelayStateBindingError(
                    "Relay state database predates account binding; move it "
                    "aside and use a new RELAY_STATE_DIR"
                )

            if directory_binding is None:
                directory_binding = self._create_directory_binding()
                if not self._binding.matches(directory_binding):
                    self._raise_mismatch()

            self._assert_state_directory()
            with db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS relay_state_account (
                      singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                      binding_schema TEXT NOT NULL,
                      api_origin TEXT NOT NULL,
                      token_fingerprint TEXT NOT NULL,
                      account_fingerprint TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    INSERT OR IGNORE INTO relay_state_account(
                      singleton,binding_schema,api_origin,
                      token_fingerprint,account_fingerprint
                    ) VALUES(1,?,?,?,?)
                    """,
                    (
                        STATE_BINDING_SCHEMA,
                        self._binding.api_origin,
                        self._binding.token_fingerprint,
                        self._binding.account_fingerprint,
                    ),
                )
                self._assert_state_directory()

            database_binding, _ = self._read_database_binding(db)
            if (
                database_binding is None
                or not self._binding.matches(database_binding)
            ):
                self._raise_mismatch()

            self._assert_state_directory()
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                self._assert_state_directory()
                self._create_schema(db)
                self._assert_state_directory()
                # A previous gateway process cannot still own its in-memory
                # Hermes task. Identity checks must happen before this requeue.
                db.execute(
                    """
                    UPDATE events
                    SET status = 'pending', updated_at = CURRENT_TIMESTAMP
                    WHERE status IN ('processing', 'dispatched')
                    """
                )
                self._assert_state_directory()
            self._assert_state_directory()
            self._db = db
            self._assert_bound()
            if os.name != "nt":
                self._chmod_state_file(STATE_FILENAME, STATE_FILE_MODE)
                self._chmod_state_file(
                    STATE_BINDING_FILENAME,
                    STATE_FILE_MODE,
                )
            return self
        except Exception:
            if db is not None:
                db.close()
            self._db = None
            self._close_state_directory()
            raise

    def _open_state_directory(self) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path.parent, flags)
        except OSError as exc:
            raise RelayStateBindingError(
                "Relay state directory must be a real directory"
            ) from exc
        self._directory_fd = descriptor
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise RelayStateBindingError(
                    "Relay state directory must be a real directory"
                )
            os.fchmod(descriptor, STATE_DIRECTORY_MODE)
            self._assert_state_directory()
        except Exception:
            self._close_state_directory()
            raise

    def _close_state_directory(self) -> None:
        if self._directory_fd is not None:
            os.close(self._directory_fd)
            self._directory_fd = None

    def _assert_state_directory(self) -> None:
        if os.name == "nt":
            return
        descriptor = self._directory_fd
        if descriptor is None:
            raise RelayStateBindingError("Relay state directory is not open")
        try:
            opened = os.fstat(descriptor)
            current = os.stat(self.path.parent, follow_symlinks=False)
        except OSError as exc:
            raise RelayStateBindingError(
                "Relay state directory was replaced during use"
            ) from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (current.st_dev, current.st_ino)
        ):
            raise RelayStateBindingError(
                "Relay state directory was replaced during use"
            )
        if stat.S_IMODE(opened.st_mode) != STATE_DIRECTORY_MODE:
            raise RelayStateBindingError(
                "Relay state directory permissions changed during use"
            )

    def _state_file_open_args(self, filename: str) -> Tuple[Any, Dict[str, Any]]:
        if os.name != "nt" and self._directory_fd is not None:
            return filename, {"dir_fd": self._directory_fd}
        return self.path.parent / filename, {}

    def _chmod_state_file(self, filename: str, mode: int) -> None:
        if os.name == "nt":
            return
        self._assert_state_directory()
        target, options = self._state_file_open_args(filename)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(target, flags, **options)
        except OSError as exc:
            raise RelayStateBindingError(
                f"Relay state file {filename!r} cannot be opened safely"
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise RelayStateBindingError(
                    f"Relay state file {filename!r} is not regular"
                )
            os.fchmod(descriptor, mode)
        finally:
            os.close(descriptor)
        self._assert_state_directory()

    @staticmethod
    def _create_schema(db: sqlite3.Connection) -> None:
        db.execute(
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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_chats (
              chat_id TEXT PRIMARY KEY,
              chat_json TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshot_messages (
              message_id TEXT PRIMARY KEY,
              chat_id TEXT NOT NULL REFERENCES snapshot_chats(chat_id)
                ON DELETE CASCADE,
              message_json TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS snapshot_messages_chat
            ON snapshot_messages(chat_id, message_id)
            """
        )
        db.execute(
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
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
              sequence TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

    def _read_directory_binding(self) -> Optional[RelayStateBinding]:
        self._assert_state_directory()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        target, options = self._state_file_open_args(STATE_BINDING_FILENAME)
        try:
            descriptor = os.open(target, flags, **options)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RelayStateBindingError(
                "Relay state account binding cannot be read safely"
            ) from exc
        try:
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 16_384:
                    raise RelayStateBindingError(
                        "Relay state account binding is malformed"
                    )
                with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                    descriptor = -1
                    raw = handle.read(16_385)
            except UnicodeError as exc:
                raise RelayStateBindingError(
                    "Relay state account binding is malformed"
                ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._assert_state_directory()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RelayStateBindingError(
                "Relay state account binding is malformed"
            ) from exc
        if not isinstance(parsed, dict):
            raise RelayStateBindingError(
                "Relay state account binding is malformed"
            )
        return RelayStateBinding.from_record(parsed)

    def _create_directory_binding(self) -> RelayStateBinding:
        binding = self._binding
        if binding is None:  # pragma: no cover - guarded by open()
            raise RelayStateBindingError("Relay state account binding is required")
        payload = (
            json.dumps(
                binding.record(),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._assert_state_directory()
        target, options = self._state_file_open_args(STATE_BINDING_FILENAME)
        try:
            descriptor = os.open(
                target,
                flags,
                STATE_FILE_MODE,
                **options,
            )
        except FileExistsError:
            raced = self._read_directory_binding()
            if raced is None:  # pragma: no cover - concurrent unlink
                raise RelayStateBindingError(
                    "Relay state account binding changed during startup"
                )
            return raced
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        self._assert_state_directory()
        return binding

    @staticmethod
    def _read_database_binding(
        db: sqlite3.Connection,
    ) -> Tuple[Optional[RelayStateBinding], set[str]]:
        try:
            objects = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            }
            if "relay_state_account" not in objects:
                return None, objects
            rows = db.execute(
                """
                SELECT binding_schema,api_origin,
                       token_fingerprint,account_fingerprint
                FROM relay_state_account
                WHERE singleton=1
                """
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RelayStateBindingError(
                "Relay state database account binding is malformed"
            ) from exc
        if len(rows) != 1:
            raise RelayStateBindingError(
                "Relay state database account binding is malformed"
            )
        row = rows[0]
        return RelayStateBinding.from_record({
            "schema": row[0],
            "api_origin": row[1],
            "token_fingerprint": row[2],
            "account_fingerprint": row[3],
        }), objects

    @staticmethod
    def _raise_mismatch() -> None:
        raise RelayStateBindingError(
            "Relay state is bound to a different API origin or Agent Token; "
            "use a separate RELAY_STATE_DIR"
        )

    def _assert_bound(self) -> None:
        db = self._db
        binding = self._binding
        if db is None:
            raise RuntimeError("Relay inbox is not open")
        if binding is None:  # pragma: no cover - open() rejects this
            raise RelayStateBindingError("Relay state account binding is required")
        self._assert_state_directory()
        directory_binding = self._read_directory_binding()
        database_binding, _ = self._read_database_binding(db)
        if (
            directory_binding is None
            or database_binding is None
            or not binding.matches(directory_binding)
            or not binding.matches(database_binding)
        ):
            self._raise_mismatch()

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
        self._close_state_directory()

    def _connection(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Relay inbox is not open")
        self._assert_bound()
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

        # Account ownership takes precedence over parsing untrusted snapshot
        # data. A mismatched process must do no snapshot processing at all.
        db = self._connection()
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
