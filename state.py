"""Durable receive state for the Relay plugin: poll cursor plus dedupe window.

Both values live in one JSON file under ``~/.hermes/relay/`` and are written
together. Keeping them in one atomic write is the point: a cursor saved
without its dedupe window would, after a crash, replay a page of events that
the handler already ran, and the plugin would answer the same message twice.

Every write goes to a temp file in the same directory and then
``os.replace``, which is atomic on POSIX and on Windows. A half-written file
therefore never becomes the file a restart reads: either the old state or the
new state is there, never a truncated mixture.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "~/.hermes/relay"
STATE_FILENAME = "state.json"
# Enough ids to cover a full page of redelivery after a restart without
# growing the file past a few tens of KB.
DEFAULT_DEDUPE_CAPACITY = 4096


class RelayState:
    """Cursor + dedupe window persisted as one file.

    The cursor is monotonic. An attempt to move it backwards is refused
    rather than obeyed: rewinding replays events the handler already ran, and
    the usual cause is a stale process, not a real rollback.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        dedupe_capacity: int = DEFAULT_DEDUPE_CAPACITY,
    ) -> None:
        self.path = Path(path).expanduser() if path else Path(DEFAULT_STATE_DIR).expanduser() / STATE_FILENAME
        self._dedupe_capacity = dedupe_capacity
        self._cursor = 0
        self._seen: List[str] = []
        self._loaded = False

    # -- load ---------------------------------------------------------------

    def load(self) -> "RelayState":
        """Read state from disk. A missing file starts at cursor 0.

        A corrupt or unreadable file is reported and treated as absent. The
        alternative, raising, leaves the agent permanently dead over a file
        that a single successful poll rewrites correctly.
        """
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self
        except OSError as exc:
            logger.warning("[relay] could not read %s: %s", self.path, exc)
            return self
        try:
            parsed: Dict[str, Any] = json.loads(raw)
            cursor = parsed.get("cursor", 0)
            if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
                raise ValueError(f"bad cursor {cursor!r}")
            seen = parsed.get("seen_event_ids") or []
            if not isinstance(seen, list):
                raise ValueError("seen_event_ids must be a list")
            self._cursor = cursor
            self._seen = [str(v) for v in seen][-self._dedupe_capacity:]
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[relay] ignoring corrupt state file %s (%s); starting from cursor 0",
                self.path, exc,
            )
        return self

    # -- accessors ----------------------------------------------------------

    @property
    def cursor(self) -> int:
        self._require_loaded()
        return self._cursor

    def seen_event_ids(self) -> List[str]:
        self._require_loaded()
        return list(self._seen)

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("relay state: load() must run before use")

    # -- mutation -----------------------------------------------------------

    def advance(self, cursor: int, seen_event_ids: Optional[List[str]] = None) -> bool:
        """Persist a forward cursor move together with the dedupe window.

        Returns True when something was written. A cursor that did not move
        forward writes nothing, so an idle long poll does not rewrite the file
        every 30 seconds.
        """
        self._require_loaded()
        if seen_event_ids is not None:
            self._seen = [str(v) for v in seen_event_ids][-self._dedupe_capacity:]
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor <= self._cursor:
            return False
        self._cursor = cursor
        self.flush()
        return True

    def flush(self) -> None:
        """Write cursor and dedupe window atomically."""
        payload = json.dumps(
            {"cursor": self._cursor, "seen_event_ids": self._seen},
            ensure_ascii=False,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(payload + "\n", encoding="utf-8")
            # 0600: the file carries conversation event ids, not secrets, but
            # it sits beside credentials in ~/.hermes and inherits its posture.
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("[relay] could not persist state to %s: %s", self.path, exc)


def default_state_path() -> Path:
    return Path(DEFAULT_STATE_DIR).expanduser() / STATE_FILENAME
