"""Relay platform adapter for Hermes Agent.

Makes a Relay Chat a Hermes platform: your Hermes appears as a Contact people
can message. Inbound Messages arrive by
Relay WebSocket, and replies go out through the v1 Chats/Messages REST API.
The always-on Hermes gateway can own this connection without exposing a
public URL.

The transport lives in :mod:`relay_api`, a straight port of Relay's canonical
TypeScript SDK, and the durable inbox in :mod:`state`. This file is only the
Hermes binding: it extends ``BasePlatformAdapter``, turns Relay events into
``MessageEvent`` objects, and turns Hermes replies into Relay messages.

Platform id is ``relayapp``, not ``relay``. Hermes ships a built-in
``Platform.RELAY`` for its own connector (gateway/config.py:348), so the
obvious name is taken; ``Platform._missing_`` mints a stable pseudo-member for
any registered plugin name, so a fresh id resolves with no core edits.

Configuration in ``~/.hermes/.env`` (or ``config.yaml`` under
``gateway.platforms.relayapp.extra``; env wins)::

    RELAY_AGENT_TOKEN          Agent Token, shown once at agent creation
    RELAY_BASE_URL             API origin (default: https://api.relayapp.im)
    RELAY_ALLOWED_CONTACTS     Optional comma-separated Contact ids
    RELAY_STATE_DIR            Durable inbox directory (default: <profile>/relay)
    RELAY_HOME_CHAT             Chat id for cron delivery
    RELAY_HOME_CHAT_NAME        Human label for the home Chat
    RELAY_REPLY_TO_MODE        off | first | all | auto (default: auto)
    RELAY_GROUP_CHAT_POLICY    mentions | all (default: mentions)

Relay authenticates senders server side. When ``RELAY_ALLOWED_CONTACTS`` is set,
only those Contact ids are passed to Hermes; otherwise every Contact that can
message the agent is accepted. Chat access never grants operator authority.
Relay slash-command messages are not dispatched to Hermes on the pinned core:
privileged command policy is not profile-aware there, so every Relay profile
fails closed while ordinary chat continues.
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import logging
import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    cache_image_from_bytes,
)
from gateway.platforms.helpers import strip_markdown

from .relay_api import (
    DEFAULT_BASE_URL,
    HTTPX_AVAILABLE,
    WEBSOCKETS_AVAILABLE,
    MAX_PARTS_PER_MESSAGE,
    MAX_TEXT_PART_UNITS,
    InboundRelayMessage,
    RelayApiError,
    RelayClient,
    RelayFullSyncError,
    RelayWebhookConfiguredError,
    RelayWebSocketDisconnect,
    RelayWebSocketError,
    mentions_agent,
    normalize_base_url,
    parse_inbound,
    render_text,
    reply_idempotency_key,
    run_websocket_loop,
    split_paragraphs,
    utf16_len,
)
from .state import (
    STATE_FILENAME,
    RelayInbox,
    RelayStateBinding,
    RelayStateBindingError,
)

logger = logging.getLogger(__name__)

PLATFORM_NAME = "relayapp"
PLATFORM_LABEL = "Relay"

MAX_MESSAGE_LENGTH = MAX_TEXT_PART_UNITS
# The current MessageContent contract allows at most 100 ordered parts.
# which would drop the whole reply on the floor.
MAX_PARTS_PER_POST = MAX_PARTS_PER_MESSAGE
# Folding never grows a text part past the same byte cap the chunker splits
# on, so a fold can never build a part the chunker itself would have taken
# apart. Tied to the constant rather than repeated: two numbers telling the
# same story drift.
MERGE_UNIT_BUDGET = MAX_TEXT_PART_UNITS
MAX_INLINE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

MEDIA_PLACEHOLDER = "[media]"
SILENCE_SENTINEL = "[no reply]"
REPLY_TO_MODES = {"off", "first", "all", "auto"}
DEFAULT_REPLY_TO_MODE = "auto"

# What the agent does with a group message it was not named in. "mentions"
# answers only when mentioned; "all" answers every group message, for an agent
# whose job really is to read the whole room. Direct messages are answered
# either way.
GROUP_CHAT_POLICIES = {"mentions", "all"}
DEFAULT_GROUP_CHAT_POLICY = "mentions"
WEBSOCKET_READY_TIMEOUT_SECONDS = 30.0

# Pinned Hermes ``gateway.slash_access.policy_for_source`` ignores
# ``source.profile`` and reads only the process runner's primary config. There
# is therefore no safe per-profile Relay operator decision. This impossible
# Relay Contact id keeps Hermes's generic slash gate enabled and denied for
# every profile as defense in depth; adapter intake also drops every slash
# message before Hermes dispatch.
NO_RELAY_SLASH_ADMIN = "!relay-hermes-slash-disabled"

@dataclasses.dataclass
class _TurnEvent:
    event_id: str
    next_ordinal: int = 0


_TURN_EVENT: contextvars.ContextVar[Optional[_TurnEvent]] = (
    contextvars.ContextVar("relay_turn_event", default=None)
)


def _profile_value(name: str) -> str:
    """Use Hermes's adapter reader for primary startup and scoped profiles."""

    return str(get_scoped_secret(name, "") or "").strip()


def _resolve(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    """Profile env wins over profile config; multiplexed misses never fall back."""

    return _profile_value(env) or str(extra.get(key, "") or "").strip() or default


def _configured_base_url(extra: Dict[str, Any]) -> str:
    return _resolve(extra, "base_url", "RELAY_BASE_URL") or _resolve(
        extra,
        "api_url",
        "RELAY_API_URL",
        DEFAULT_BASE_URL,
    )


def _coerce_contact_ids(raw: Any) -> set[str]:
    """Normalize a Contact-id sequence or comma-separated scalar."""

    if isinstance(raw, (list, tuple, set, frozenset)):
        return {
            str(contact_id).strip()
            for contact_id in raw
            if str(contact_id).strip()
        }
    return {
        contact_id.strip()
        for contact_id in str(raw or "").split(",")
        if contact_id.strip()
    }


def _resolve_contact_allowlist(extra: Dict[str, Any]) -> set[str]:
    """Resolve ordinary chat access independently from slash authority."""

    key = "allowed_contacts"
    scoped = _profile_value("RELAY_ALLOWED_CONTACTS")
    if scoped:
        return _coerce_contact_ids(scoped)
    if key in extra:
        return _coerce_contact_ids(extra.get(key))
    return set()


def _install_access_policy(config: PlatformConfig) -> set[str]:
    """Preserve ordinary chat while forcing every Relay slash command closed.

    Hermes's central chat authorization understands ``allowed_users`` while
    its slash authorization reads only the primary profile config. Mirroring
    the chat principals keeps normal Contact chat working. Overwriting every
    admin and user command list with a deny policy prevents either a primary or
    secondary Relay source from inheriting a cross-profile `/update` grant.
    """

    extra = dict(getattr(config, "extra", {}) or {})
    config.extra = extra
    allowed_contacts = _resolve_contact_allowlist(extra)

    # The adapter enforces allowed_contacts before dispatch. Mirroring the
    # same decision here lets Hermes's central chat gate accept only messages
    # that this profile's adapter admitted. "*" preserves Relay's documented
    # default that every reachable Contact may chat.
    chat_principals = sorted(allowed_contacts) or ["*"]
    extra["allowed_users"] = chat_principals
    extra["allow_from"] = chat_principals
    extra["group_allow_from"] = chat_principals
    # Retire both the former plugin vocabulary and Hermes-native attempted
    # grants. Pinned Hermes cannot enforce them per source.profile.
    extra.pop("operator_contacts", None)
    extra["allow_admin_from"] = [NO_RELAY_SLASH_ADMIN]
    extra["group_allow_admin_from"] = [NO_RELAY_SLASH_ADMIN]
    extra["user_allowed_commands"] = []
    extra["group_user_allowed_commands"] = []
    return allowed_contacts


def _is_slash_message(text: str) -> bool:
    """Return whether Relay must withhold this message from Hermes dispatch."""

    return bool((text or "").lstrip().startswith("/"))


_CHUNK_INDICATOR = re.compile(r"\s*\(\d+/\d+\)$")


def _strip_chunk_indicators(chunks: List[str]) -> List[str]:
    """Remove the base splitter's `` (1/3)`` suffixes.

    Relay bubbles flow naturally without pagination furniture.
    """
    return [_CHUNK_INDICATOR.sub("", chunk) for chunk in chunks]


def _bubble_chunks(content: str) -> List[str]:
    """One text part per thought.

    Blank lines split bubbles first; only a paragraph still past Relay's
    10,000 UTF-16-unit cap falls back to the length splitter.
    """
    chunks: List[str] = []
    for paragraph in split_paragraphs(content):
        if utf16_len(paragraph) <= MAX_MESSAGE_LENGTH:
            chunks.append(paragraph)
        else:
            chunks.extend(
                _strip_chunk_indicators(
                    BasePlatformAdapter.truncate_message(
                        paragraph, max_length=MAX_MESSAGE_LENGTH, len_fn=utf16_len
                    )
                )
            )
    return chunks


def _fold_parts(parts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Preserve paragraphs without posting forbidden adjacent text parts.

    Media boundaries stay intact. Text that cannot merge within the UTF-16
    limit stays separate here; _message_batches gives it a new Message boundary.
    """
    folded: List[Dict[str, Any]] = []
    for part in parts:
        last = dict(part)
        prev = folded[-1] if folded else None
        if (
            prev is not None
            and prev.get("type") == last.get("type") == "text"
            and utf16_len(prev["value"]) + 2 + utf16_len(last["value"])
            <= MERGE_UNIT_BUDGET
        ):
            prev["value"] = f"{prev['value']}\n\n{last['value']}"
        else:
            folded.append(last)
    return folded


def _message_batches(parts: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Fold paragraphs and partition in order within MessageContent limits."""
    batches: List[List[Dict[str, Any]]] = []
    batch: List[Dict[str, Any]] = []
    url_media_count = 0
    for part in _fold_parts(parts):
        is_url_media = part.get("type") == "media" and bool(part.get("url"))
        if batch and (
            len(batch) == MAX_PARTS_PER_POST
            or (is_url_media and url_media_count == 40)
            or batch[-1].get("type") == part.get("type") == "text"
        ):
            batches.append(batch)
            batch = []
            url_media_count = 0
        batch.append(part)
        url_media_count += int(is_url_media)
    if batch:
        batches.append(batch)
    return batches


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
    """Durable WebSocket in, idempotent v1 Chats/Messages REST out.

    Replies commit as canonical Messages. The adapter deliberately implements
    no reactions, edits, typing indicators, or other Message effects.

    A Message contains up to 100 ordered text or media parts. Group messages
    are answered only when their structured mention names this agent, unless
    ``RELAY_GROUP_CHAT_POLICY=all``.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    # send() chunks long replies itself, so gateway/delivery.py must hand over
    # the full content untruncated.
    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))

        self._allowed_contacts = _install_access_policy(config)
        extra = config.extra
        self._base_url = normalize_base_url(_configured_base_url(extra))
        self._token = _resolve(extra, "token", "RELAY_AGENT_TOKEN")

        configured_state_dir = _resolve(
            extra,
            "state_dir",
            "RELAY_STATE_DIR",
        )
        if configured_state_dir:
            state_dir = Path(configured_state_dir).expanduser()
        else:
            # Hermes installs a context-local home override around every
            # multiplexed profile adapter. Resolve the default at construction
            # time so profile B can never inherit profile A's ~/.hermes/relay.
            from hermes_cli.config import get_hermes_home

            state_dir = Path(get_hermes_home()) / "relay"
        binding = (
            RelayStateBinding.for_account(self._base_url, self._token)
            if self._token
            else None
        )
        self._inbox = RelayInbox(
            state_dir / STATE_FILENAME,
            binding=binding,
        )

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

        # What this agent does with a group message it was not named in.
        # Anything unrecognised reads as "mentions": a typo in a var must not
        # be what turns an agent into one that answers every message in every
        # group.
        policy = _resolve(
            extra, "group_chat_policy", "RELAY_GROUP_CHAT_POLICY",
        ).strip().lower()
        if policy and policy not in GROUP_CHAT_POLICIES:
            logger.warning(
                "[%s] unknown group_chat_policy %r, using %r (valid: %s)",
                self.name, policy, DEFAULT_GROUP_CHAT_POLICY,
                ", ".join(sorted(GROUP_CHAT_POLICIES)),
            )
            policy = ""
        self._group_chat_policy = policy or DEFAULT_GROUP_CHAT_POLICY

        self._client: Optional[RelayClient] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._process_task: Optional[asyncio.Task] = None
        self._inbox_wake = asyncio.Event()
        self._websocket_ready = asyncio.Event()

        # chat_id -> newest inbound message id, for "auto" quoting.
        self._last_inbound: Dict[str, str] = {}
        # Fire-and-forget Read receipts. asyncio keeps only weak references
        # to tasks, so the set holds each one until it finishes.
        self._read_tasks: set = set()
        # Chat kind, learned directly from each Relay Message event.
        self._group_chats: set = set()
        self._direct_chats: set = set()
    # -- Connection lifecycle ----------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect when this Agent has no Webhook subscription."""
        if not HTTPX_AVAILABLE:
            logger.warning("[%s] httpx is not installed", self.name)
            return False
        if not WEBSOCKETS_AVAILABLE:
            logger.warning("[%s] websockets is not installed", self.name)
            return False
        if not self._token:
            logger.warning("[%s] RELAY_AGENT_TOKEN is not set", self.name)
            return False

        try:
            # The durable sink must be available before Relay can send an event
            # that this adapter will cumulatively acknowledge.
            self._inbox.open()
            self._apply_full_snapshot(self._inbox.load_full_snapshot())
            self._client = RelayClient(self._token, self._base_url)
            self._client.open()
        except RelayStateBindingError as error:
            self._set_fatal_error(
                "relay_state_account_mismatch",
                str(error),
                retryable=False,
            )
            logger.error("[%s] %s", self.name, error)
            await self._close_client()
            self._inbox.close()
            return False
        except RelayWebhookConfiguredError as error:
            self._set_fatal_error(
                "relay_webhook_configured",
                str(error),
                retryable=False,
            )
            logger.error("[%s] %s", self.name, error)
            await self._close_client()
            self._inbox.close()
            return False
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
            self._inbox.close()
            return False
        except Exception as exc:
            logger.error("[%s] failed to connect: %s", self.name, exc)
            await self._close_client()
            self._inbox.close()
            return False

        self._websocket_ready.clear()
        self._running = True
        self._receive_task = asyncio.create_task(self._run_websocket())
        ready_task = asyncio.create_task(self._websocket_ready.wait())
        try:
            await asyncio.wait(
                {ready_task, self._receive_task},
                timeout=WEBSOCKET_READY_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not self._websocket_ready.is_set() or self._receive_task.done():
                if not self.has_fatal_error:
                    logger.warning("[%s] WebSocket did not become ready", self.name)
                await self.disconnect()
                return False
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        finally:
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
        self._process_task = asyncio.create_task(self._process_inbox())
        self._inbox_wake.set()
        self._mark_connected()
        logger.info(
            "[%s] connected to %s by Relay WebSocket",
            self.name, self._base_url,
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()

        for task in (self._receive_task, self._process_task):
            if task:
                task.cancel()
        for task in (self._receive_task, self._process_task):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._receive_task = None
        self._process_task = None
        self._inbox.close()

        await self._close_client()
        self._direct_chats.clear()
        self._group_chats.clear()
        self._last_inbound.clear()
        logger.info("[%s] disconnected", self.name)

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- Receive -----------------------------------------------------------

    def _allow_contact(self, contact_id: str) -> bool:
        return not self._allowed_contacts or contact_id in self._allowed_contacts

    async def _run_websocket(self) -> None:
        assert self._client is not None
        try:
            await run_websocket_loop(
                client=self._client,
                inbox=self._inbox,
                on_accepted=self._inbox_wake.set,
                on_ready=self._websocket_ready.set,
                on_full_sync=self._on_full_sync,
                should_continue=lambda: self._running,
                log=lambda line: logger.warning("[%s] %s", self.name, line),
            )
        except asyncio.CancelledError:
            return
        except RelayApiError as error:
            if isinstance(error, RelayWebhookConfiguredError):
                self._set_fatal_error(
                    "relay_webhook_configured",
                    str(error),
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
                    "relay_websocket_terminal",
                    f"Relay WebSocket stopped: {error}",
                    retryable=False,
            )
            self._running = False
        except RelayFullSyncError as error:
            self._set_fatal_error(
                "relay_full_sync_failed",
                f"Relay FULL sync stopped safely: {error}",
                retryable=False,
            )
            self._running = False
        except RelayWebSocketDisconnect as error:
            if error.reason == "webhook_configured":
                self._set_fatal_error(
                    "relay_webhook_configured",
                    (
                        "This Agent now delivers by webhook; remove every "
                        "webhook subscription before reconnecting."
                    ),
                    retryable=False,
                )
            else:
                self._set_fatal_error(
                    f"relay_websocket_{error.reason}",
                    str(error),
                    retryable=False,
                )
            self._running = False
        except RelayWebSocketError as error:
            self._set_fatal_error(
                f"relay_websocket_{error.code}",
                f"Relay WebSocket stopped: {error}",
                retryable=False,
            )
            self._running = False
        except Exception as exc:
            self._set_fatal_error(
                "relay_websocket_stopped",
                f"Relay WebSocket stopped: {exc}",
                retryable=False,
            )
        finally:
            self._inbox_wake.set()

    async def _on_full_sync(
        self,
        through_sequence: str,
        reason: str,
    ) -> None:
        """Replace durable Relay state before confirming a required FULL sync."""

        if reason != "checkpoint_outside_retention":
            raise RelayFullSyncError(
                f"unsupported Relay FULL sync reason {reason!r}"
            )
        client = self._client
        if client is None:
            raise RelayFullSyncError(
                "Relay client is unavailable during FULL sync."
            )
        snapshot = await client.fetch_full_snapshot()
        try:
            groups, directs, latest_inbound = self._snapshot_indexes(snapshot)
            self._inbox.replace_full_snapshot(
                through_sequence,
                snapshot,
            )
            self._group_chats = groups
            self._direct_chats = directs
            self._last_inbound = latest_inbound
        except RelayFullSyncError:
            raise
        except (TypeError, ValueError) as exc:
            raise RelayFullSyncError(
                f"the REST snapshot could not be reconciled: {exc}"
            ) from exc

    def _apply_full_snapshot(
        self,
        snapshot: List[Dict[str, Any]],
    ) -> None:
        """Rebuild the adapter's Chat and latest-inbound indexes."""

        groups, directs, latest_inbound = self._snapshot_indexes(snapshot)
        self._group_chats = groups
        self._direct_chats = directs
        self._last_inbound = latest_inbound

    @staticmethod
    def _snapshot_indexes(
        snapshot: List[Dict[str, Any]],
    ) -> Tuple[set[str], set[str], Dict[str, str]]:
        """Validate a complete snapshot before changing live adapter state."""

        groups: set[str] = set()
        directs: set[str] = set()
        latest_inbound: Dict[str, str] = {}
        for entry in snapshot:
            chat = entry.get("chat") if isinstance(entry, dict) else None
            messages = entry.get("messages") if isinstance(entry, dict) else None
            chat_id = chat.get("id") if isinstance(chat, dict) else None
            is_group = chat.get("is_group") if isinstance(chat, dict) else None
            if (
                not isinstance(chat_id, str)
                or not chat_id
                or not isinstance(is_group, bool)
                or not isinstance(messages, list)
            ):
                raise RelayFullSyncError(
                    "Relay REST snapshot contains a Chat the Hermes adapter "
                    "cannot safely reconcile."
                )
            (groups if is_group else directs).add(chat_id)
            inbound: List[Tuple[str, str]] = []
            for message in messages:
                message_id = (
                    message.get("id") if isinstance(message, dict) else None
                )
                if (
                    not isinstance(message, dict)
                    or not isinstance(message_id, str)
                    or message.get("chat_id") != chat_id
                    or not isinstance(message.get("is_from_me"), bool)
                    or not isinstance(message.get("is_system_message"), bool)
                    or not isinstance(message.get("created_at"), str)
                ):
                    raise RelayFullSyncError(
                        "Relay REST snapshot contains a Message the Hermes "
                        "adapter cannot safely reconcile."
                    )
                if (
                    message["is_from_me"] is False
                    and message.get("is_system_message") is not True
                ):
                    inbound.append((message["created_at"], message_id))
            if inbound:
                latest_inbound[chat_id] = max(inbound)[1]

        return groups, directs, latest_inbound

    async def _process_inbox(self) -> None:
        # Hermes installs the handler after connect returns. Durable events stay
        # pending until it exists.
        while self._running and self._message_handler is None:
            await asyncio.sleep(0.1)
        while self._running:
            # Clear before the query. If a commit races this check, either the
            # query sees it or the subsequent wait observes the set Event.
            self._inbox_wake.clear()
            pending = self._inbox.next_pending()
            if pending is None:
                await self._inbox_wake.wait()
                continue
            event_id, event = pending
            try:
                # Mark the handoff before calling handle_message(). Hermes can
                # start and finish a fast background turn before it returns.
                self._inbox.mark_dispatched(event_id)
                inbound = parse_inbound(event)
                if inbound is None:
                    self._inbox.complete(event_id, ignored=True)
                    continue
                if not self._allow_contact(inbound.sender_contact_id):
                    self._inbox.complete(event_id, ignored=True)
                    continue
                dispatched = await self._on_inbound(inbound)
                if not dispatched:
                    self._inbox.complete(event_id, ignored=True)
            except asyncio.CancelledError:
                self._inbox.retry(event_id, "interrupted")
                raise
            except Exception as exc:
                self._inbox.retry(event_id, str(exc))
                logger.exception("[%s] Relay event %s failed", self.name, event_id)
                await asyncio.sleep(1.0)

    def _addressed_in_group(self, inbound: InboundRelayMessage) -> bool:
        """Match a structured mention against ``chat.owner_handle``."""
        if self._group_chat_policy == "all":
            return True
        return mentions_agent(
            inbound.message,
            handle=inbound.agent_handle,
        )

    async def _on_inbound(self, inbound: InboundRelayMessage) -> bool:
        """Turn one Relay event into a Hermes ``MessageEvent``."""
        chat_id = inbound.chat_id
        if chat_id and inbound.message_id:
            if len(self._last_inbound) > 500:
                self._last_inbound.pop(next(iter(self._last_inbound)), None)
            self._last_inbound[chat_id] = inbound.message_id

        is_group = inbound.is_group
        cache = self._group_chats if is_group else self._direct_chats
        if len(cache) > 1000:
            cache.clear()
        cache.add(chat_id)
        chat_type = "group" if is_group else "dm"

        if is_group and not self._addressed_in_group(inbound):
            logger.debug(
                "[%s] not mentioned in group %s, staying out of %s",
                self.name, chat_id, inbound.message_id,
            )
            return False

        text = render_text(inbound.message)
        if _is_slash_message(text):
            # Pinned Hermes resolves slash policy from the runner's primary
            # config and ignores source.profile. Do not let either a primary
            # or secondary Relay Contact reach that unsafe dispatch seam.
            logger.info(
                "[%s] Relay slash commands are disabled; ignoring %s",
                self.name,
                inbound.message_id,
            )
            return False
        media_paths, media_kinds, notes = await self._ingest_media(inbound.message)
        if notes:
            text = "\n".join(filter(None, [text, *notes]))
        if not text and not media_paths:
            logger.debug("[%s] message %s carries no readable content", self.name, inbound.message_id)
            return False

        source = self.build_source(
            chat_id=chat_id,
            chat_name=f"Relay Chat {chat_id}",
            chat_type=chat_type,
            # Hermes names these normalized adapter fields user_id/user_name.
            # Their Relay values are the sending Contact id and display name.
            user_id=inbound.sender_contact_id or "unknown",
            user_name=(
                inbound.sender_contact_name
                or inbound.sender_contact_id
                or "unknown"
            ),
            message_id=inbound.message_id,
        )

        event = MessageEvent(
            text=text or MEDIA_PLACEHOLDER,
            message_type=MessageType.PHOTO if media_paths else MessageType.TEXT,
            source=source,
            user_id=inbound.sender_contact_id or None,
            user_name=inbound.sender_contact_name or None,
            message_id=inbound.message_id or inbound.event_id,
            raw_message=inbound.event,
            timestamp=self._parse_timestamp(inbound.created_at),
            media_urls=media_paths,
            media_types=media_kinds,
        )

        # Read at intake, the moment the message is accepted for Hermes, as
        # the BlueBubbles adapter does for iMessage (mark_read right after
        # handing the event over, before any busy routing). Hermes folds a
        # message that arrives mid-turn into the running turn under its
        # default busy_input_mode: interrupt and never calls a processing
        # hook for it, so a Read tied to on_processing_start left every
        # folded message at Delivered forever. Fire-and-forget: a failed
        # receipt never blocks intake.
        self._mark_read_in_background(chat_id)
        await self._dispatch_turn(event)
        return True

    def _mark_read_in_background(self, chat_id: str) -> None:
        if self._client is None or not chat_id:
            return
        task = asyncio.create_task(self._mark_read(chat_id))
        self._read_tasks.add(task)
        task.add_done_callback(self._read_tasks.discard)

    async def _mark_read(self, chat_id: str) -> None:
        try:
            await self._client.mark_read(chat_id)
        except Exception as exc:  # noqa: BLE001 - a receipt never breaks intake
            logger.debug("[%s] Read receipt failed: %s", self.name, exc)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Bind the turn when Hermes actually starts processing.

        Read is not sent here: this hook never runs for a message Hermes
        folded into an already-running turn. Intake sends it instead.
        """

        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        event_id = str(raw.get("event_id") or "")
        _TURN_EVENT.set(_TurnEvent(event_id) if event_id else None)

    async def on_processing_complete(
        self,
        event: MessageEvent,
        outcome: ProcessingOutcome,
    ) -> None:
        """Keep the durable row until Hermes finishes delivery."""

        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        event_id = str(raw.get("event_id") or "")
        if event_id:
            if outcome == ProcessingOutcome.SUCCESS:
                self._inbox.complete(event_id)
            else:
                self._inbox.retry(event_id, f"Hermes processing {outcome.value}")
                asyncio.get_running_loop().call_later(1.0, self._inbox_wake.set)
        _TURN_EVENT.set(None)

    async def _dispatch_turn(self, event: MessageEvent) -> None:
        """Hand one accepted turn to Hermes."""
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        event_id = str(raw.get("event_id") or "")
        token = _TURN_EVENT.set(_TurnEvent(event_id) if event_id else None)
        try:
            # handle_message creates the background turn task here. asyncio
            # copies this Context into that task, so overlapping Chats cannot
            # steal one another's idempotency keys.
            await self.handle_message(event)
        finally:
            _TURN_EVENT.reset(token)

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
        if client is None or client._transfer_http is None:  # noqa: SLF001 - same package
            return paths, kinds, notes

        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind != "media":
                continue
            url = part.get("url")
            if not url:
                notes.append("[attachment received but no download URL was provided]")
                continue
            mime_type = str(part.get("mime_type") or "").lower()
            if mime_type.startswith("audio/"):
                seconds = (part.get("duration_ms") or 0) / 1000.0
                notes.append(
                    f"[voice memo received ({seconds:.0f}s), transcription is not wired up]"
                    if seconds
                    else "[voice memo received, transcription is not wired up]"
                )
                continue
            # Stream images so a large file is bounded while downloading.
            try:
                async with client._transfer_http.stream("GET", url, timeout=60.0) as resp:  # noqa: SLF001
                    resp.raise_for_status()
                    ctype = mime_type or (
                        (resp.headers.get("content-type") or "")
                        .lower().split(";")[0].strip()
                    )
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

    def _next_idempotency_key(
        self,
        chat_id: str,
        _ordinal: int,
        _content: Dict[str, Any],
    ) -> str:
        """Return one stable key per logical POST in a Hermes turn."""

        entry = _TURN_EVENT.get()
        if entry is None:
            return f"hermes-{chat_id}-{time.time_ns()}"
        # Child asyncio tasks inherit the same turn object. Replacing a tuple
        # in their copied Contexts would allocate ordinal zero in every child
        # and leave the parent's counter unchanged. No await separates this
        # allocation from its increment.
        next_ordinal = entry.next_ordinal
        entry.next_ordinal += 1
        # A retry can regenerate different words. The logical operation is
        # still event + send ordinal; changing the key with the body could
        # create a duplicate message instead of surfacing an idempotency
        # conflict.
        return reply_idempotency_key(entry.event_id, next_ordinal)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send one or more text parts into a Relay Chat."""
        if self._client is None:
            return SendResult(success=False, error="Relay client is not connected")
        if not chat_id:
            return SendResult(success=False, error="no Chat id")

        content = self.format_message(content)

        # Model-chosen silence. People do not answer every "ok cool", and an
        # agent forced to emit something emits filler. Silence stays DM-only:
        # a group turn only reaches here because the agent was named, and being
        # named and then saying nothing reads as broken rather than tactful.
        if self._is_silence(content) and chat_id not in self._group_chats:
            logger.info("[%s] model chose not to reply in %s", self.name, chat_id)
            await self.stop_typing(chat_id)
            return SendResult(success=True, message_id=None)
        if self._is_silence(content):
            content = "OK"

        parts = [{"type": "text", "value": chunk} for chunk in _bubble_chunks(content)]
        if not parts:
            return SendResult(success=False, error="nothing to send")
        return await self._commit(chat_id, parts, reply_to)

    @staticmethod
    def _is_silence(content: str) -> bool:
        stripped = (content or "").strip()
        return not stripped or stripped.lower() == SILENCE_SENTINEL

    @staticmethod
    def truncate_message(
        content: str,
        max_length: int = MAX_MESSAGE_LENGTH,
        len_fn=None,
    ) -> List[str]:
        # The base splitter appends " (1/3)" indicators when a reply spans
        # chunks; Relay bubbles do not carry pagination furniture.
        return _strip_chunk_indicators(
            BasePlatformAdapter.truncate_message(content, max_length, len_fn=len_fn)
        )

    def format_message(self, content: str) -> str:
        """Relay renders plain text, so strip the markdown a model emits.

        Headings and bold markers are chrome from a docs-shaped model; in a
        messenger bubble they read as literal asterisks. The chunker still
        needs fences intact, so code blocks survive untouched.
        """
        if not content or "```" in content:
            return content
        return strip_markdown(content)

    def _should_thread_reply(self, chat_id: str, reply_to: Optional[str]) -> bool:
        """Honor ``PlatformConfig.reply_to_mode``, as the Telegram adapter does.

        | mode          | behavior                                            |
        |---------------|-----------------------------------------------------|
        | `off`         | never quote                                         |
        | `first`/`all` | always quote (a turn is one POST, so they agree)    |
        | `auto`        | quote only when it disambiguates (this plugin's default) |

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
        if mode == "auto":
            if chat_id in self._group_chats:
                return True
            return reply_to != self._last_inbound.get(chat_id)
        return True

    def _reply_anchor(
        self, chat_id: str, reply_to: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        if not self._should_thread_reply(chat_id, reply_to):
            return None
        # Hermes gives this adapter a message-level reply anchor.
        return {"message_id": reply_to}

    async def _commit(
        self, chat_id: str, parts: List[Dict[str, Any]], reply_to: Optional[str]
    ) -> SendResult:
        """Send ordered parts in current MessageContent batches."""
        first: Optional[SendResult] = None
        for ordinal, batch in enumerate(_message_batches(parts)):
            result = await self._post_message(
                chat_id,
                batch,
                reply_to=self._reply_anchor(chat_id, reply_to) if ordinal == 0 else None,
                idempotency_ordinal=ordinal,
            )
            if not result.success:
                return result
            if first is None:
                first = result
        return first or SendResult(success=False, error="nothing to send")

    async def _post_message(
        self,
        chat_id: str,
        parts: List[Dict[str, Any]],
        reply_to: Optional[Dict[str, Any]] = None,
        idempotency_ordinal: int = 0,
    ) -> SendResult:
        """POST one current SendMessageToChatRequest. Never raises."""
        assert self._client is not None
        content = {"message": {"parts": parts}}
        if reply_to:
            content["message"]["reply_to"] = reply_to
        try:
            body = await self._client.send_message(
                chat_id, parts,
                idempotency_key=self._next_idempotency_key(
                    chat_id,
                    idempotency_ordinal,
                    {"chat_id": chat_id, **content},
                ),
                reply_to=reply_to,
            )
            sent = body.get("message")
            message_id = sent.get("id") if isinstance(sent, dict) else None
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
        return SendResult(success=True, message_id=message_id)

    # -- Outbound media ----------------------------------------------------

    async def _upload_attachment(self, file_path: str) -> Tuple[Optional[str], Optional[str]]:
        """Allocate an Attachment, then PUT the raw bytes."""
        if self._client is None:
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
            attachment_id = await self._client.upload_attachment(
                filename=Path(safe).name,
                content_type=content_type,
                data=data,
            )
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
        # A caption follows the media as its own text part in the same Message.
        parts: List[Dict[str, Any]] = [part]
        if caption:
            parts.extend({"type": "text", "value": chunk} for chunk in _bubble_chunks(caption))
        return await self._commit(chat_id, parts, reply_to)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Ship contiguous media parts in contract-bounded Message batches.

        Contiguous media parts commit as one stacked media message, which is
        how Relay renders a photo set; the base implementation loops one send
        per image and would land N separate bubbles. Alt texts follow the
        stack as text parts so the media stays contiguous. ``human_delay`` is
        accepted for signature parity and ignored.
        """
        parts: List[Dict[str, Any]] = []
        captions: List[str] = []
        for image_url, alt_text in images:
            if image_url.startswith("file://"):
                attachment_id, error = await self._upload_attachment(unquote(image_url[7:]))
                if not attachment_id:
                    logger.error("[%s] image upload failed: %s", self.name, error)
                    continue
                parts.append({"type": "media", "attachment_id": attachment_id})
            else:
                parts.append({"type": "media", "url": image_url})
            if alt_text:
                captions.append(alt_text)
        if not parts:
            return
        for caption in captions:
            parts.extend({"type": "text", "value": chunk} for chunk in _bubble_chunks(caption))
        result = await self._commit(chat_id, parts, None)
        if not result.success:
            logger.error("[%s] failed to send image batch: %s", self.name, result.error)

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

    async def send_voice(
        self,
        chat_id,
        audio_path,
        caption=None,
        reply_to=None,
        metadata=None,
        **kwargs,
    ) -> SendResult:
        """Send audio as an idempotent Message with one media part."""

        return await self.send_document(
            chat_id,
            audio_path,
            caption=caption,
            reply_to=reply_to,
        )

    # Hermes calls these hooks. They intentionally remain no-ops: Relay-Hermes
    # does not emit typing or other Message effects.

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id: str) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "name": chat_id,
            "type": "group" if chat_id in self._group_chats else "dm",
        }


# ---------------------------------------------------------------------------
# Registration surface
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive probe: is the plugin installable and minimally configured?

    Called from status displays and config loading, so it must never install
    anything.
    """
    if (
        not HTTPX_AVAILABLE
        or not WEBSOCKETS_AVAILABLE
        or not _profile_value("RELAY_AGENT_TOKEN")
    ):
        return False
    try:
        normalize_base_url(_configured_base_url({}))
    except ValueError:
        return False
    return True


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    if not _resolve(extra, "token", "RELAY_AGENT_TOKEN"):
        return False
    try:
        normalize_base_url(_configured_base_url(extra))
    except ValueError:
        return False
    return True


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed profile-scoped config without mutating process-global auth state."""

    token = _profile_value("RELAY_AGENT_TOKEN")
    if not token:
        return None
    seed: dict = {"token": token}
    base_url = _profile_value("RELAY_BASE_URL")
    if base_url:
        # Preserve configured input. RelayAdapter normalizes a valid origin and
        # rejects an invalid one; mutating "/" to empty here would fall back to
        # the production default.
        seed["base_url"] = base_url
    raw_allowed_contacts = _profile_value("RELAY_ALLOWED_CONTACTS")
    if raw_allowed_contacts:
        allowed_contacts = _coerce_contact_ids(raw_allowed_contacts)
        seed["allowed_contacts"] = sorted(allowed_contacts)
        seed["allowed_users"] = sorted(allowed_contacts)
    state_dir = _profile_value("RELAY_STATE_DIR")
    if state_dir:
        seed["state_dir"] = state_dir
    home = _profile_value("RELAY_HOME_CHAT")
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": _profile_value("RELAY_HOME_CHAT_NAME") or home,
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
    ``thread_id`` and ``media_files`` are Hermes adapter parameters; this path
    sends text parts only.
    """
    if not HTTPX_AVAILABLE:
        return {"error": "relay standalone send: httpx is not installed"}
    extra = getattr(pconfig, "extra", {}) or {}
    token = _resolve(extra, "token", "RELAY_AGENT_TOKEN")
    base_url = _configured_base_url(extra)
    if not token:
        return {"error": "relay standalone send: RELAY_AGENT_TOKEN is not set"}
    if not chat_id:
        return {
            "error": "relay standalone send: no Chat id (set RELAY_HOME_CHAT)"
        }

    parts = [{"type": "text", "value": chunk} for chunk in _bubble_chunks(message)]
    if not parts:
        return {"error": "relay standalone send: nothing to send"}
    try:
        first: Optional[Dict[str, Any]] = None
        async with RelayClient(token, base_url) as client:
            # Cron text is fixed for a run, so the key is derived from the
            # target and the send time rather than an event id. A POST carries
            # at most 100 parts; anything folding cannot fit
            # ships as successive POSTs. The ordinal is in the key as well as
            # the clock, so two POSTs cannot collide on a coarse timer and
            # have the server dedupe the second away.
            stamp = time.time_ns()
            for index, batch in enumerate(_message_batches(parts)):
                body = await client.send_message(
                    chat_id,
                    batch,
                    idempotency_key=f"hermes-cron-{chat_id}-{stamp}-{index}",
                )
                if first is None:
                    first = body
        # Report the FIRST committed message, the same end of the run
        # ``_commit`` reports, so callers key on one convention.
        sent = (first or {}).get("message")
        message_id = sent.get("id") if isinstance(sent, dict) else None
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
        install_hint="pip install httpx websockets",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="RELAY_HOME_CHAT",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="RELAY_ALLOWED_CONTACTS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="\N{EIGHT SPOKED ASTERISK}",
        # Relay Contact ids are server-authenticated UUIDs; no
        # phone numbers or email addresses cross this adapter.
        pii_safe=True,
        # Pinned Hermes slash policy is not source.profile-aware. Even if a
        # MessageEvent bypassed adapter intake, /update must reject relayapp.
        allow_update_command=False,
        platform_hint=PLATFORM_HINT,
    )
