"""Relay platform adapter for Hermes Agent.

Makes a Relay agent conversation a Hermes channel: your Hermes shows up in
Relay as a contact people text like a friend. Inbound messages arrive by
long-polling Relay's durable event log (``GET /v1/events``), replies go out
through ``POST /v1/messages``. Polling needs no webhook, no public URL and no
signing secret, which is what makes this work on a laptop behind NAT.

The transport lives in :mod:`relay_api`, a straight port of Relay's canonical
TypeScript SDK, and the durable cursor in :mod:`state`. This file is only the
Hermes binding: it extends ``BasePlatformAdapter``, turns Relay events into
``MessageEvent`` objects, and turns Hermes replies into Relay messages.

Platform id is ``relayapp``, not ``relay``. Hermes ships a built-in
``Platform.RELAY`` for its own connector (gateway/config.py:348), so the
obvious name is taken; ``Platform._missing_`` mints a stable pseudo-member for
any registered plugin name, so a fresh id resolves with no core edits.

Configuration in ``~/.hermes/.env`` (or ``config.yaml`` under
``platforms.relayapp.extra``; env wins)::

    RELAY_AGENT_TOKEN          Agent Token, shown once at agent creation
    RELAY_BASE_URL             API origin (default: https://api.relayapp.im)
    RELAY_ALLOWED_USERS        Extra Relay user ids (usr_...) beyond the owner
    RELAY_ALLOW_ALL_USERS      Let anyone who can reach the agent talk to it
    RELAY_STATE_DIR            Cursor + dedupe directory (default: ~/.hermes/relay)
    RELAY_HOME_CHANNEL         Conversation id (cnv_...) for cron delivery
    RELAY_HOME_CHANNEL_NAME    Human label for the home channel
    RELAY_REPLY_TO_MODE        off | first | all | auto (default: auto)

Security model. Relay authenticates senders server side, so ``sender.id``
(``usr_...``) is a real identity and is safe to authorize on. By default this
adapter answers only the agent's owner: it reads ``owner_user_id`` from
``GET /v1/agents/me`` at connect and drops every other sender before the
message reaches the model. Opening the agent to other people is an explicit
choice, made with ``RELAY_ALLOWED_USERS`` or ``RELAY_ALLOW_ALL_USERS``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import mimetypes
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
)
from gateway.platforms.helpers import TextBatchAggregator, strip_markdown

from .relay_api import (
    DEFAULT_BASE_URL,
    HTTPX_AVAILABLE,
    MAX_POLL_TIMEOUT_SECONDS,
    MAX_TEXT_PART_BYTES,
    InboundMessage,
    MemoryDedupe,
    RelayApiError,
    RelayClient,
    is_consumer_takeover,
    normalize_base_url,
    render_text,
    reply_idempotency_key,
    run_poll_loop,
)
from .state import DEFAULT_STATE_DIR, STATE_FILENAME, RelayState

logger = logging.getLogger(__name__)

PLATFORM_NAME = "relayapp"
PLATFORM_LABEL = "Relay"

MAX_MESSAGE_LENGTH = MAX_TEXT_PART_BYTES
MAX_INLINE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

# Rapid-fire messages merge into one turn instead of racing separate turns,
# the pattern every mature Hermes adapter implements (feishu/matrix/wecom).
TEXT_BATCH_DELAY_SECONDS = 0.6
TEXT_BATCH_SPLIT_DELAY_SECONDS = 2.0
TEXT_BATCH_SPLIT_THRESHOLD = 4000
INTER_CHUNK_DELAY_SECONDS = 0.4

SILENCE_SENTINEL = "[no reply]"
REPLY_TO_MODES = {"off", "first", "all", "auto"}
DEFAULT_REPLY_TO_MODE = "auto"
# Relay expires a group invocation server side; holding a dead id past this
# only produces rejected sends.
INVOCATION_TTL_SECONDS = 15 * 60


def _resolve(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    """Env wins over ``config.yaml`` extras; both stripped."""
    return os.getenv(env, "").strip() or str(extra.get(key, "") or "").strip() or default


def _is_true(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _resolve_authz(extra: Dict[str, Any], key: str, env: str) -> str:
    """Authorization settings resolve from extras first, not from env.

    This is the one place the usual env-wins rule is inverted, and it has to
    be. :func:`_env_enablement` records the operator's real choice in extras
    and then sets ``RELAY_ALLOW_ALL_USERS=true`` in the environment so the
    Hermes-wide authorization layer defers to this adapter. Reading env first
    here would read that hand-off back and silently turn the owner-only
    default into allow-all.
    """
    if key in extra:
        return str(extra.get(key) or "").strip()
    return os.getenv(env, "").strip()


def _explicit_reply_to_mode(config) -> str:
    """The operator's ``reply_to_mode``, or empty when they never set one.

    ``PlatformConfig.reply_to_mode`` is a first-class field that already
    carries the Hermes-wide default of "first" whether or not anybody chose
    it. Reading it directly would mean this plugin's documented "auto"
    default could never take effect. Comparing against the dataclass default
    tells an inherited value apart from a deliberate one.
    """
    value = str(getattr(config, "reply_to_mode", "") or "").strip()
    if not value:
        return ""
    for field_def in dataclasses.fields(config):
        if field_def.name == "reply_to_mode" and value == field_def.default:
            return ""
    return value


class RelayAdapter(BasePlatformAdapter):
    """Long-poll ``/v1/events`` in, ``POST /v1/messages`` out.

    Replies commit as canonical messages. Relay has no live partial bubble and
    no message-edit endpoint on the developer API, so ``SUPPORTS_MESSAGE_EDITING``
    stays off and the stream consumer skips the progressive-edit path.

    Groups differ from DMs in one way that shapes the whole send path: a group
    ``message.received`` carries ``data.invocation_id``, the reply and any
    typing signal must carry it back, and the first committed reply completes
    the invocation. A group turn therefore ships as ONE message with several
    ordered text parts, while a DM turn may ship as several messages.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    SUPPORTS_MESSAGE_EDITING = False
    # send() chunks long replies itself, so gateway/delivery.py must hand over
    # the full content untruncated.
    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))

        extra = config.extra or {}
        raw_base = _resolve(extra, "base_url", "RELAY_BASE_URL") or _resolve(
            extra, "api_url", "RELAY_API_URL", DEFAULT_BASE_URL
        )
        try:
            self._base_url = normalize_base_url(raw_base)
        except ValueError as exc:
            logger.error("[%s] %s", self.name, exc)
            self._base_url = DEFAULT_BASE_URL
        self._token = _resolve(extra, "token", "RELAY_AGENT_TOKEN")

        state_dir = Path(
            _resolve(extra, "state_dir", "RELAY_STATE_DIR", DEFAULT_STATE_DIR)
        ).expanduser()
        self._state = RelayState(state_dir / STATE_FILENAME)

        mode = (
            _resolve(extra, "reply_to_mode", "RELAY_REPLY_TO_MODE")
            or _explicit_reply_to_mode(config)
            or DEFAULT_REPLY_TO_MODE
        ).strip().lower()
        if mode not in REPLY_TO_MODES:
            logger.warning(
                "[%s] unknown reply_to_mode %r, using %r (valid: %s)",
                self.name, mode, DEFAULT_REPLY_TO_MODE, ", ".join(sorted(REPLY_TO_MODES)),
            )
            mode = DEFAULT_REPLY_TO_MODE
        self._reply_to_mode = mode

        self._allow_all = _is_true(_resolve_authz(extra, "allow_all_users", "RELAY_ALLOW_ALL_USERS"))
        self._allowed_users = {
            entry.strip()
            for entry in _resolve_authz(extra, "allowed_users", "RELAY_ALLOWED_USERS").split(",")
            if entry.strip()
        }

        self._client: Optional[RelayClient] = None
        self._agent_id = ""
        self._agent_handle = ""
        self._owner_user_id = ""
        self._poll_task: Optional[asyncio.Task] = None
        self._dedupe = MemoryDedupe()

        # conversation_id -> (invocation id, received-at). Groups only.
        self._invocations: Dict[str, Tuple[str, float]] = {}
        # conversation_id -> newest inbound message id, for "auto" quoting.
        self._last_inbound: Dict[str, str] = {}
        self._group_convs: set = set()
        # event id of the message currently being answered, so replies derive
        # a stable Idempotency-Key instead of a random one.
        self._reply_keys: Dict[str, Tuple[str, int]] = {}

        self._text_batcher = TextBatchAggregator(
            handler=self.handle_message,
            batch_delay=TEXT_BATCH_DELAY_SECONDS,
            split_delay=TEXT_BATCH_SPLIT_DELAY_SECONDS,
            split_threshold=TEXT_BATCH_SPLIT_THRESHOLD,
        )

    # -- Connection lifecycle ----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Verify the Agent Token, load the cursor, start the poll task."""
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx not installed. Run: pip install httpx", self.name)
            return False
        if not self._token:
            logger.warning("[%s] RELAY_AGENT_TOKEN is not set", self.name)
            return False

        try:
            self._client = RelayClient(self._token, self._base_url)
            self._client.open()
            agent = await self._client.get_me()
        except RelayApiError as error:
            if error.kind == "auth":
                self._set_fatal_error(
                    "relay_unauthorized",
                    "Relay rejected the Agent Token (401). Check RELAY_AGENT_TOKEN.",
                    retryable=False,
                )
                logger.error("[%s] Agent Token rejected (401)", self.name)
            else:
                logger.error("[%s] could not reach Relay: %s", self.name, error)
            await self._close_client()
            return False
        except Exception as exc:
            logger.error("[%s] failed to connect: %s", self.name, exc)
            await self._close_client()
            return False

        self._agent_id = str(agent.get("id") or "")
        self._agent_handle = str(agent.get("handle") or "")
        self._owner_user_id = str(agent.get("owner_user_id") or "")

        self._state.load()
        self._dedupe.restore(self._state.seen_event_ids())

        if not self._owner_user_id and not self._allow_all and not self._allowed_users:
            logger.warning(
                "[%s] Relay did not report an owner for @%s and no allowlist is set, "
                "so every inbound message will be dropped. Set RELAY_ALLOWED_USERS "
                "or RELAY_ALLOW_ALL_USERS to choose who may talk to this agent.",
                self.name, self._agent_handle or self._agent_id,
            )

        self._poll_task = asyncio.create_task(self._run_poll_loop())
        self._mark_connected()
        logger.info(
            "[%s] connected as @%s (%s) via %s, cursor %d",
            self.name, self._agent_handle, self._agent_id, self._base_url, self._state.cursor,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        # Persist the dedupe window next to the cursor already on disk, so a
        # restart does not re-answer the last page of events.
        try:
            self._state.advance(self._state.cursor, self._dedupe.snapshot())
            self._state.flush()
        except RuntimeError:
            pass  # never loaded: nothing to persist

        await self._close_client()
        self._text_batcher.cancel_all()
        self._invocations.clear()
        self._group_convs.clear()
        self._last_inbound.clear()
        self._reply_keys.clear()
        logger.info("[%s] disconnected", self.name)

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Receive -----------------------------------------------------------

    def _allow_sender(self, sender_id: str) -> bool:
        """Owner-only by default.

        Relay's own reference plugin keys on ``owner_user_id`` from
        ``GET /v1/agents/me``. An agent that runs shell tools on somebody's
        laptop is not a public endpoint, so anyone else is dropped before the
        text reaches the model, and opening it up is an explicit choice.
        """
        if self._allow_all:
            return True
        if sender_id and sender_id in self._allowed_users:
            return True
        return bool(self._owner_user_id) and sender_id == self._owner_user_id

    async def _run_poll_loop(self) -> None:
        # The gateway installs the message handler only after connect()
        # returns. Events already waiting in the log would otherwise be
        # fetched now, dropped by handle_message (a no-op without a handler),
        # and permanently acknowledged by the next cursor save.
        for _ in range(300):
            if self._message_handler is not None or not self._running:
                break
            await asyncio.sleep(0.1)

        assert self._client is not None
        try:
            await run_poll_loop(
                client=self._client,
                get_cursor=lambda: self._state.cursor,
                set_cursor=self._save_cursor,
                dedupe=self._dedupe,
                on_message=self._on_inbound,
                allow_sender=self._allow_sender,
                should_continue=lambda: self._running,
                timeout_seconds=MAX_POLL_TIMEOUT_SECONDS,
                log=lambda line: logger.warning("[%s] %s", self.name, line),
            )
        except asyncio.CancelledError:
            return
        except RelayApiError as error:
            if is_consumer_takeover(error):
                self._set_fatal_error(
                    "relay_consumer_takeover",
                    "Another consumer took this Relay Agent Token. Stop the other "
                    "process, or issue this Hermes its own agent and token.",
                    retryable=False,
                )
            elif error.kind == "auth":
                self._set_fatal_error(
                    "relay_unauthorized",
                    "Relay rejected the Agent Token (401). Check RELAY_AGENT_TOKEN.",
                    retryable=False,
                )
            else:
                self._set_fatal_error(
                    "relay_poll_terminal", f"Relay long poll stopped: {error}",
                    retryable=False,
                )
            self._running = False
        except Exception as exc:
            logger.error("[%s] poll loop stopped: %s", self.name, exc)
            self._running = False

    def _save_cursor(self, cursor: int) -> None:
        self._state.advance(cursor, self._dedupe.snapshot())

    async def _on_inbound(self, inbound: InboundMessage) -> None:
        """Turn one Relay event into a Hermes ``MessageEvent``."""
        conversation_id = inbound.conversation_id
        if conversation_id and inbound.message_id:
            if len(self._last_inbound) > 500:
                self._last_inbound.pop(next(iter(self._last_inbound)), None)
            self._last_inbound[conversation_id] = inbound.message_id

        if inbound.invocation_id and conversation_id:
            if len(self._invocations) > 200:
                self._invocations.pop(next(iter(self._invocations)), None)
            self._invocations[conversation_id] = (inbound.invocation_id, time.time())
            if len(self._group_convs) > 1000:
                self._group_convs.clear()
            self._group_convs.add(conversation_id)

        is_group = inbound.is_group or conversation_id in self._group_convs
        chat_type = "group" if is_group else "dm"

        text = render_text(inbound.message)
        media_paths, media_kinds, notes = await self._ingest_media(inbound.message)
        if notes:
            text = "\n".join(filter(None, [text, *notes]))
        if not text and not media_paths:
            logger.debug("[%s] message %s carries no readable content", self.name, inbound.message_id)
            return

        # Bind the reply key to the conversation before dispatch: send() runs
        # later, on the gateway's turn, and needs this event's id to derive a
        # stable Idempotency-Key.
        if conversation_id and inbound.event_id:
            self._reply_keys[conversation_id] = (inbound.event_id, 0)

        source = self.build_source(
            chat_id=conversation_id,
            chat_name=f"Relay conversation {conversation_id}",
            chat_type=chat_type,
            user_id=inbound.sender_id or "unknown",
            user_name=(inbound.message.get("sender") or {}).get("display_name")
            or inbound.sender_id
            or "unknown",
            message_id=inbound.message_id,
        )

        event = MessageEvent(
            text=text or "[media]",
            message_type=MessageType.PHOTO if media_paths else MessageType.TEXT,
            source=source,
            user_id=inbound.sender_id or None,
            user_name=(inbound.message.get("sender") or {}).get("display_name"),
            message_id=inbound.message_id or inbound.event_id,
            raw_message=inbound.event,
            timestamp=self._parse_timestamp(inbound.message.get("created_at")),
            media_urls=media_paths,
            media_types=media_kinds,
        )

        # Plain text bursts batch per conversation. Anything carrying media or
        # a group invocation dispatches immediately: an invocation has a TTL
        # and must not sit in a debounce window.
        if (
            event.message_type == MessageType.TEXT
            and not media_paths
            and not inbound.invocation_id
            and self._text_batcher.is_enabled()
        ):
            self._text_batcher.enqueue(event, conversation_id)
            return
        await self.handle_message(event)

    async def _ingest_media(
        self, message: Dict[str, Any]
    ) -> Tuple[List[str], List[str], List[str]]:
        """Download inbound media through its capability URL.

        Images land in the Hermes image cache and come back as local paths in
        ``media_urls`` so the vision tools can read them. Anything else
        becomes an explicit placeholder line instead of vanishing silently.

        Capability URLs are secrets. They are passed to the fetch and never
        logged.
        """
        paths: List[str] = []
        kinds: List[str] = []
        notes: List[str] = []
        client = self._client
        if client is None or client._http is None:  # noqa: SLF001 - same package
            return paths, kinds, notes

        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind not in ("media", "voice_memo"):
                continue
            url = part.get("url")
            if not url:
                notes.append("[attachment received but no download URL was provided]")
                continue
            if kind == "voice_memo":
                seconds = (part.get("duration_ms") or 0) / 1000.0
                notes.append(
                    f"[voice memo received ({seconds:.0f}s), transcription is not wired up]"
                    if seconds
                    else "[voice memo received, transcription is not wired up]"
                )
                continue
            # Canonical parts carry no content type, so the response header is
            # the only signal. Stream, so a 100 MB video is classified from its
            # headers instead of being downloaded to find out.
            try:
                async with client._http.stream("GET", url, timeout=60.0) as resp:  # noqa: SLF001
                    resp.raise_for_status()
                    ctype = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
                    if not ctype.startswith("image/"):
                        notes.append(f"[attachment received: {ctype or 'file'}]")
                        continue
                    declared = int(resp.headers.get("content-length") or 0)
                    if declared > MAX_INLINE_IMAGE_BYTES:
                        notes.append(f"[image too large to inspect ({declared // 1048576} MB)]")
                        continue
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > MAX_INLINE_IMAGE_BYTES:
                            break
                if len(buf) > MAX_INLINE_IMAGE_BYTES:
                    notes.append("[image too large to inspect]")
                    continue
                ext_map = {
                    "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
                    "image/heic": ".heic", "image/heif": ".heic",
                }
                paths.append(cache_image_from_bytes(bytes(buf), ext=ext_map.get(ctype, ".jpg")))
                kinds.append(ctype)
            except ValueError:
                notes.append("[attachment received]")
            except Exception as exc:
                logger.warning("[%s] media download failed: %s", self.name, type(exc).__name__)
                notes.append("[image attachment could not be downloaded]")
        return paths, kinds, notes

    @staticmethod
    def _parse_timestamp(created_at: Optional[str]) -> datetime:
        if created_at:
            try:
                return datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(tz=timezone.utc)

    # -- Send --------------------------------------------------------------

    def _next_idempotency_key(self, chat_id: str) -> str:
        """A stable key per (event, reply ordinal).

        A retry after a timeout re-sends the same key, so Relay commits the
        message once. Falls back to a time-based key only when the reply is
        not answering a known event, for example a cron delivery.
        """
        entry = self._reply_keys.get(chat_id)
        if entry is None:
            return f"hermes-{chat_id}-{time.time_ns()}"
        event_id, ordinal = entry
        self._reply_keys[chat_id] = (event_id, ordinal + 1)
        return reply_idempotency_key(event_id, ordinal)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text reply into a Relay conversation.

        DMs send each chunk as its own message, because separate bubbles read
        the way people text. Groups send one message with several ordered text
        parts, because the first committed reply completes the invocation.
        """
        if self._client is None:
            return SendResult(success=False, error="Relay client is not connected")
        if not chat_id:
            return SendResult(success=False, error="no conversation id")

        content = self.format_message(content)

        # Model-chosen silence. People do not answer every "ok cool", and an
        # agent forced to emit something emits filler. A group invocation is
        # an explicit call for a response and staying quiet leaves it pending,
        # so silence is DM-only.
        if self._is_silence(content) and chat_id not in self._group_convs:
            logger.info("[%s] model chose not to reply in %s", self.name, chat_id)
            await self.stop_typing(chat_id)
            return SendResult(success=True, message_id=None)
        if self._is_silence(content):
            content = "OK"

        chunks = self.truncate_message(content, max_length=self.MAX_MESSAGE_LENGTH)

        if chat_id in self._group_convs:
            parts = [{"type": "text", "text": chunk} for chunk in chunks]
            return await self._commit_group_message(chat_id, parts, reply_to=reply_to)

        last = SendResult(success=False, error="nothing to send")
        for index, chunk in enumerate(chunks):
            if index:
                # Pace multi-bubble replies so they read like typing.
                await asyncio.sleep(INTER_CHUNK_DELAY_SECONDS)
            result = await self._post_message(
                chat_id,
                [{"type": "text", "text": chunk}],
                reply_to=self._reply_anchor(chat_id, reply_to, index),
            )
            if not result.success:
                return result
            last = result
        return last

    @staticmethod
    def _is_silence(content: str) -> bool:
        stripped = (content or "").strip()
        return not stripped or stripped.lower() == SILENCE_SENTINEL

    def format_message(self, content: str) -> str:
        """Relay renders plain text, so strip the markdown a model emits.

        Headings and bold markers are chrome from a docs-shaped model; in a
        messenger bubble they read as literal asterisks. The chunker still
        needs fences intact, so code blocks survive untouched.
        """
        if not content or "```" in content:
            return content
        return strip_markdown(content)

    def _should_thread_reply(self, chat_id: str, reply_to: Optional[str], chunk_index: int) -> bool:
        """Honor ``PlatformConfig.reply_to_mode``, as the Telegram adapter does.

        | mode    | behavior                                                  |
        |---------|-----------------------------------------------------------|
        | `off`   | never quote                                               |
        | `first` | quote the first chunk only                                |
        | `all`   | quote every chunk                                         |
        | `auto`  | quote only when it disambiguates (this plugin's default)  |

        ``auto`` matches how people use replies in a messenger: answering the
        newest message needs no quote, but once something newer arrived the
        quote says which question is being answered. Group turns always quote,
        because with several speakers the anchor is what makes it readable.
        """
        if not reply_to:
            return False
        mode = self._reply_to_mode
        if mode == "off":
            return False
        if mode == "all":
            return True
        if mode == "auto":
            if chat_id in self._group_convs:
                return True
            return reply_to != self._last_inbound.get(chat_id)
        return chunk_index == 0

    def _reply_anchor(
        self, chat_id: str, reply_to: Optional[str], chunk_index: int = 0
    ) -> Optional[Dict[str, Any]]:
        if not self._should_thread_reply(chat_id, reply_to, chunk_index):
            return None
        return {"message_id": reply_to, "part_index": 0}

    def _pending_invocation(self, chat_id: str) -> Optional[str]:
        entry = self._invocations.get(chat_id)
        if not entry:
            return None
        invocation_id, received_at = entry
        if time.time() - received_at > INVOCATION_TTL_SECONDS:
            # The server already expired it; attaching a dead id only earns a
            # rejection.
            self._invocations.pop(chat_id, None)
            return None
        return invocation_id

    async def _commit_group_message(
        self,
        chat_id: str,
        parts: List[Dict[str, Any]],
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Commit the one reply a group invocation allows.

        The invocation is dropped on success or on a definitive 403 (consumed,
        expired, or membership ended). A transient failure keeps it, so the
        retry still carries it.
        """
        invocation_id = self._pending_invocation(chat_id)
        if not invocation_id:
            logger.info(
                "[%s] suppressing group send with no pending invocation (%s)",
                self.name, chat_id,
            )
            return SendResult(success=False, error="no pending invocation for this group turn")
        result = await self._post_message(
            chat_id, parts,
            reply_to=self._reply_anchor(chat_id, reply_to),
            invocation_id=invocation_id,
        )
        if result.success or result.error and result.error.startswith("HTTP 403"):
            self._invocations.pop(chat_id, None)
        return result

    async def _post_message(
        self,
        chat_id: str,
        parts: List[Dict[str, Any]],
        reply_to: Optional[Dict[str, Any]] = None,
        invocation_id: Optional[str] = None,
    ) -> SendResult:
        """POST one canonical message. Never raises."""
        assert self._client is not None
        try:
            body = await self._client.send_message(
                chat_id, parts,
                idempotency_key=self._next_idempotency_key(chat_id),
                reply_to=reply_to,
                invocation_id=invocation_id,
            )
        except RelayApiError as error:
            logger.warning("[%s] send failed: %s", self.name, error)
            return SendResult(
                success=False,
                error=f"HTTP {error.status}: {error}" if error.status else str(error),
                retryable=error.retryable,
            )
        except Exception as exc:
            logger.error("[%s] send error: %s", self.name, exc)
            return SendResult(success=False, error=str(exc))
        message_id = body.get("message_id") or (body.get("message") or {}).get("id")
        return SendResult(success=True, message_id=message_id)

    # -- Outbound media ----------------------------------------------------

    async def _upload_attachment(self, file_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Upload raw bytes to ``POST /v1/attachments`` -> (attachment_id, error)."""
        if self._client is None or self._client._http is None:  # noqa: SLF001
            return None, "Relay client is not connected"
        safe = self.validate_media_delivery_path(file_path)
        if not safe:
            return None, f"path not allowed for upload: {file_path}"
        content_type = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        try:
            data = Path(safe).read_bytes()
        except OSError as exc:
            return None, f"cannot read {safe}: {exc}"
        if len(data) > MAX_ATTACHMENT_BYTES:
            return None, "file exceeds Relay's 100 MB attachment limit"
        try:
            resp = await self._client._http.post(  # noqa: SLF001
                f"{self._base_url}/v1/attachments",
                content=data,
                headers={"Content-Type": content_type, "X-Relay-Filename": Path(safe).name},
                timeout=120.0,
            )
            if resp.status_code >= 300:
                return None, f"upload HTTP {resp.status_code}"
            attachment_id = (resp.json().get("attachment") or {}).get("id")
            if not attachment_id:
                return None, "upload returned no attachment id"
            return attachment_id, None
        except Exception as exc:
            return None, f"upload failed: {exc}"

    async def _send_media_part(
        self,
        chat_id: str,
        part: Dict[str, Any],
        caption: Optional[str],
        reply_to: Optional[str],
    ) -> SendResult:
        parts: List[Dict[str, Any]] = []
        if caption:
            parts.append({"type": "text", "text": caption[: self.MAX_MESSAGE_LENGTH]})
        parts.append(part)
        if chat_id in self._group_convs:
            return await self._commit_group_message(chat_id, parts, reply_to=reply_to)
        return await self._post_message(chat_id, parts, reply_to=self._reply_anchor(chat_id, reply_to))

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult:
        """Send a hosted image as a native media part."""
        return await self._send_media_part(chat_id, {"type": "media", "url": image_url}, caption, reply_to)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        attachment_id, error = await self._upload_attachment(image_path)
        if not attachment_id:
            return SendResult(success=False, error=error)
        return await self._send_media_part(
            chat_id, {"type": "media", "attachment_id": attachment_id}, caption, reply_to,
        )

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        attachment_id, error = await self._upload_attachment(file_path)
        if not attachment_id:
            return SendResult(success=False, error=error)
        return await self._send_media_part(
            chat_id, {"type": "media", "attachment_id": attachment_id}, caption, reply_to,
        )

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self.send_document(chat_id, video_path, caption=caption, reply_to=reply_to)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        """Upload audio and send it as a native voice memo with an inline player."""
        content_type = mimetypes.guess_type(audio_path)[0] or ""
        if not content_type.startswith("audio/"):
            # Relay validates the audio/* family for voice memos; anything
            # else ships as a regular attachment.
            return await self.send_document(chat_id, audio_path, caption=caption, reply_to=reply_to)
        attachment_id, error = await self._upload_attachment(audio_path)
        if not attachment_id:
            return SendResult(success=False, error=error)
        return await self._send_media_part(
            chat_id, {"type": "voice_memo", "attachment_id": attachment_id}, caption, reply_to,
        )

    # -- Typing ------------------------------------------------------------

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Light the typing indicator on the user's devices.

        Hermes calls this on a refresh cadence with a short budget, so the
        timeout stays tight and a failure never raises: a missed tick only
        delays the indicator.
        """
        await self._typing(chat_id, True, metadata=metadata, timeout=1.5)

    async def stop_typing(self, chat_id: str) -> None:
        await self._typing(chat_id, False, timeout=5.0)

    async def _typing(
        self, chat_id: str, started: bool, metadata=None, timeout: float = 5.0
    ) -> None:
        if self._client is None or not chat_id:
            return
        invocation_id = None
        if chat_id in self._group_convs:
            # Group typing is rejected without the pending invocation, and once
            # the reply consumed it there is nothing left to signal.
            invocation_id = self._pending_invocation(chat_id)
            if not invocation_id:
                return
        label = None
        if started:
            raw = (metadata or {}).get("typing_label") or (metadata or {}).get("label")
            if isinstance(raw, str) and raw.strip():
                label = raw.strip()[:80]
        try:
            await self._client.set_typing(
                chat_id, started, label=label, invocation_id=invocation_id, timeout=timeout,
            )
        except Exception as exc:
            logger.debug("[%s] typing signal failed: %s", self.name, type(exc).__name__)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "name": chat_id,
            "type": "group" if chat_id in self._group_convs else "dm",
        }


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive probe: is the plugin installable and minimally configured?

    Called from status displays and config loading, so it must never install
    anything.
    """
    return HTTPX_AVAILABLE and bool(os.getenv("RELAY_AGENT_TOKEN", "").strip())


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(_resolve(extra, "token", "RELAY_AGENT_TOKEN"))


def is_connected(config) -> bool:
    return validate_config(config)


# Operator intent is captured once. Hermes loads config more than once in one
# process. Writing RELAY_ALLOW_ALL_USERS into os.environ made the second load
# treat owner-only as allow-all.
_OPERATOR_ALLOW_ALL = os.getenv("RELAY_ALLOW_ALL_USERS", "").strip()
_OPERATOR_ALLOWED_USERS = os.getenv("RELAY_ALLOWED_USERS", "").strip()


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load."""
    token = os.getenv("RELAY_AGENT_TOKEN", "").strip()
    if not token:
        return None
    # Hermes' generic authorization layer is fail-closed and cannot know the
    # Relay owner id, which is only readable from GET /v1/agents/me at connect.
    # Authorization therefore belongs to the adapter, which drops non-owners
    # before handle_message (see RelayAdapter._allow_sender). The generic layer
    # is handed allow-all so the two do not deny twice; this widens nothing,
    # because the adapter has already applied the operator's real choice.
    seed: dict = {
        "token": token,
        "base_url": os.getenv("RELAY_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        "allow_all_users": _OPERATOR_ALLOW_ALL,
        "allowed_users": _OPERATOR_ALLOWED_USERS,
    }
    os.environ["RELAY_ALLOW_ALL_USERS"] = "true"
    state_dir = os.getenv("RELAY_STATE_DIR", "").strip()
    if state_dir:
        seed["state_dir"] = state_dir
    home = os.getenv("RELAY_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("RELAY_HOME_CHANNEL_NAME", home),
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for ``hermes send`` and cron ``deliver=relayapp``.

    Runs where no gateway adapter is live, so it opens its own short client.
    ``thread_id`` and ``media_files`` are accepted for signature parity; this
    path sends text parts only.
    """
    if not HTTPX_AVAILABLE:
        return {"error": "relay standalone send: httpx is not installed"}
    extra = getattr(pconfig, "extra", {}) or {}
    token = _resolve(extra, "token", "RELAY_AGENT_TOKEN")
    base_url = _resolve(extra, "base_url", "RELAY_BASE_URL") or _resolve(
        extra, "api_url", "RELAY_API_URL", DEFAULT_BASE_URL
    )
    if not token:
        return {"error": "relay standalone send: RELAY_AGENT_TOKEN is not set"}
    if not chat_id:
        return {"error": "relay standalone send: no conversation id (set RELAY_HOME_CHANNEL)"}

    chunks = BasePlatformAdapter.truncate_message(message, max_length=MAX_MESSAGE_LENGTH)
    try:
        message_id = None
        async with RelayClient(token, base_url) as client:
            for index, chunk in enumerate(chunks):
                # Cron text is fixed for a run, so the key is derived from the
                # target and the send time rather than an event id.
                key = f"hermes-cron-{chat_id}-{time.time_ns()}-{index}"
                body = await client.send_text(chat_id, chunk, idempotency_key=key)
                message_id = body.get("message_id") or message_id
        return {
            "success": True,
            "platform": PLATFORM_NAME,
            "chat_id": chat_id,
            "message_id": message_id,
        }
    except RelayApiError as error:
        return {"error": f"relay standalone send failed: {error}"}
    except Exception as exc:
        return {"error": f"relay standalone send failed: {exc}"}


PLATFORM_HINT = (
    "You are texting inside Relay, a messenger where you appear as a contact. "
    "Write like a person texting: short, direct messages, plain text, no "
    "markdown headings. Split long content into a few short messages rather "
    "than one wall of text. You do not have to answer every message. When a "
    "message needs no response, a simple acknowledgement like \"ok\", "
    "\"thanks\", or someone signing off, reply with exactly "
    f"{SILENCE_SENTINEL} and nothing else, and Relay will stay quiet instead "
    "of sending filler. Answer normally whenever there is a question, a "
    "request, or anything genuinely worth saying."
)


def register(ctx) -> None:
    """Plugin entry point, called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name=PLATFORM_NAME,
        label=PLATFORM_LABEL,
        adapter_factory=lambda cfg: RelayAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["RELAY_AGENT_TOKEN"],
        install_hint="pip install httpx   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="RELAY_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="RELAY_ALLOWED_USERS",
        allow_all_env="RELAY_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="\N{EIGHT SPOKED ASTERISK}",
        # Relay sender ids are server-authenticated opaque ids (usr_...); no
        # phone numbers or email addresses cross this adapter.
        pii_safe=True,
        allow_update_command=True,
        platform_hint=PLATFORM_HINT,
    )
