"""Relay v1 developer-API transport for the Hermes platform plugin.

This module is a direct port of Relay's canonical TypeScript SDK
(``@relaymessenger/sdk``, ``cli/packages/sdk/src/``) into Python. It carries
the transport semantics and nothing else: no Hermes imports live here, so the
whole receive path is testable without a gateway.

Ported piece by piece:

* ``url.py`` origin validation  -> :func:`normalize_base_url` (url.ts)
* ``errors.ts`` status classes  -> :class:`RelayApiError`, :func:`classify_status`
* ``idempotency.ts``            -> :func:`reply_idempotency_key`
* ``memory-dedupe.ts``          -> :class:`MemoryDedupe`
* ``client.ts``                 -> :class:`RelayClient`
* ``poll-loop.ts``              -> :func:`run_poll_loop`

Transport contract, in one paragraph. Inbound events arrive from
``GET /v1/events?cursor=N&timeout=30``, a long poll that needs no webhook and
no public URL, so a machine behind NAT works. Delivery is at-least-once: the
consumer deduplicates by ``event_id`` and records an id only after the handler
has succeeded, and the cursor advances only after every event on the page was
handled, so a failure replays rather than drops. Replies go out on
``POST /v1/messages`` under an ``Idempotency-Key`` derived from the event id
and the reply ordinal, so a retry after a timeout commits once. Webhooks and
long polling are mutually exclusive per Agent Token; a second consumer taking
the token ends this one with ``409 terminated_by_other_consumer``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random as _random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

try:  # httpx is a core Hermes dependency (pyproject: httpx[socks]==0.28.1).
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on a broken install
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.relayapp.im"

# poll-loop.ts: TRANSIENT_BASE_DELAY_MS / TRANSIENT_MAX_DELAY_MS.
TRANSIENT_BASE_DELAY_MS = 500
TRANSIENT_MAX_DELAY_MS = 30_000
TRANSIENT_JITTER_MS = 250
# poll-loop.ts caps the doubling exponent at 6 (500ms * 2**6 = 32s, clamped
# to the 30s ceiling), so the ladder stops climbing instead of running away.
TRANSIENT_MAX_EXPONENT = 6

# client.ts clamps the long-poll window to the server's accepted range.
MIN_POLL_TIMEOUT_SECONDS = 1
MAX_POLL_TIMEOUT_SECONDS = 30
# client.ts: pollEvents allows timeoutSeconds + 15 before giving up locally.
POLL_READ_MARGIN_SECONDS = 15
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0

# Relay caps a text part at 8 KB.
MAX_TEXT_PART_BYTES = 8000


# ---------------------------------------------------------------------------
# Errors (port of errors.ts)
# ---------------------------------------------------------------------------


class RelayApiError(Exception):
    """A classified Relay API failure.

    ``terminal`` means retrying the identical request cannot succeed. The poll
    loop retries only ``retryable`` failures and surfaces everything else.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: Optional[int] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.details = details or {}

    @property
    def terminal(self) -> bool:
        return self.kind != "retryable"

    @property
    def retryable(self) -> bool:
        return self.kind == "retryable"


def classify_status(status: int) -> str:
    """errors.ts ``classifyRelayHttpStatus``, value for value."""
    if status == 401:
        return "auth"
    if status == 409:
        return "conflict"
    if status == 408 or status == 429 or status >= 500:
        return "retryable"
    return "rejected"


def is_consumer_takeover(error: BaseException) -> bool:
    """True when a newer consumer claimed this Agent Token.

    Relay allows one long-poll consumer per token. When a second one starts,
    the older poll ends with ``409 terminated_by_other_consumer``. Restarting
    does not win the slot back, so the caller stops cleanly and says why.
    """
    return isinstance(error, RelayApiError) and error.code == "terminated_by_other_consumer"


# ---------------------------------------------------------------------------
# Base URL validation (port of url.ts)
# ---------------------------------------------------------------------------


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.strip().lower().strip("[]")
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        pass
    return normalized == "localhost" or normalized.endswith(".localhost")


def normalize_base_url(raw: Optional[str]) -> str:
    """Validate and canonicalize the API origin before a bearer token is sent.

    Remote origins must be HTTPS. Plain HTTP is accepted only on loopback, for
    local development. A URL carrying credentials, a path, a query, or a
    fragment is rejected rather than silently trimmed: a bearer token must not
    be posted to a surprising place because a config string had a typo.
    """
    candidate = (raw or "").strip() or DEFAULT_BASE_URL
    parts = urlsplit(candidate)
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"relay: invalid base URL {candidate!r}")
    if parts.username or parts.password:
        raise ValueError("relay: base URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("relay: base URL must not contain a query or fragment")
    if parts.path not in ("", "/"):
        raise ValueError("relay: base URL must be an origin without a path")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and _is_loopback_host(parts.hostname)
    ):
        raise ValueError(
            "relay: base URL must use HTTPS (HTTP is allowed only for loopback development)"
        )
    return f"{parts.scheme}://{parts.netloc}"


# ---------------------------------------------------------------------------
# Idempotency (port of idempotency.ts)
# ---------------------------------------------------------------------------


def reply_idempotency_key(event_id: str, ordinal: int = 0) -> str:
    """``replyIdempotencyKey`` from idempotency.ts: ``reply-<event_id>-<n>``.

    The nth reply to one event always derives the same key, so a send retried
    after a timeout or a redelivered event commits exactly one message. A
    random key per attempt looks equivalent and is not: it turns every retry
    into a duplicate bubble on the user's screen.
    """
    return f"reply-{event_id}-{ordinal}"


# ---------------------------------------------------------------------------
# Dedupe window (port of memory-dedupe.ts)
# ---------------------------------------------------------------------------


class MemoryDedupe:
    """Bounded insertion-ordered ``event_id`` window for at-least-once delivery."""

    def __init__(self, capacity: int = 4096) -> None:
        if capacity < 1:
            raise ValueError("relay dedupe capacity must be a positive integer")
        self._capacity = capacity
        self._seen: Dict[str, None] = {}

    def has(self, event_id: str) -> bool:
        return event_id in self._seen

    def record(self, event_id: str) -> None:
        """Record only once the event was handled. See :func:`run_poll_loop`."""
        if not event_id:
            return
        self._seen[event_id] = None
        while len(self._seen) > self._capacity:
            self._seen.pop(next(iter(self._seen)))

    def snapshot(self) -> List[str]:
        return list(self._seen)

    def restore(self, event_ids: Sequence[str]) -> None:
        for event_id in event_ids:
            self.record(event_id)

    def clear(self) -> None:
        self._seen.clear()

    def __len__(self) -> int:
        return len(self._seen)


# ---------------------------------------------------------------------------
# Client (port of client.ts)
# ---------------------------------------------------------------------------


@dataclass
class RelayResponse:
    """One HTTP result, in the shape the client needs.

    A plain dataclass rather than an httpx type so tests can drive the client
    through a fake transport with no network and no httpx import.
    """

    status: int
    body: Any = None
    text: str = ""


Transport = Callable[..., Awaitable[RelayResponse]]


@dataclass
class EventsPage:
    events: List[Dict[str, Any]] = field(default_factory=list)
    next_cursor: int = 0


class RelayClient:
    """Typed calls against the Relay v1 developer API.

    ``transport`` exists for tests: pass an async callable and no socket is
    ever opened. Left unset, the client builds an ``httpx.AsyncClient``.
    """

    def __init__(
        self,
        token: str,
        base_url: Optional[str] = None,
        *,
        transport: Optional[Transport] = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not (token or "").strip():
            raise ValueError("relay: Agent Token is required")
        self._token = token.strip()
        self.base_url = normalize_base_url(base_url)
        self._request_timeout = request_timeout
        self._transport = transport
        self._http: Optional[Any] = None

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "RelayClient":
        self.open()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def open(self) -> None:
        if self._transport is not None or self._http is not None:
            return
        if not HTTPX_AVAILABLE:
            raise RuntimeError("relay: httpx is required (pip install httpx)")
        self._http = httpx.AsyncClient(
            headers={"authorization": f"Bearer {self._token}"},
            timeout=httpx.Timeout(
                connect=15.0,
                read=MAX_POLL_TIMEOUT_SECONDS + POLL_READ_MARGIN_SECONDS,
                write=15.0,
                pool=15.0,
            ),
        )

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # -- request plumbing ---------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> RelayResponse:
        if self._transport is not None:
            response = await self._transport(
                method=method, path=path, query=query or {},
                body=body, headers=headers or {}, timeout=timeout,
            )
        else:
            response = await self._http_request(
                method, path, query or {}, body, headers or {}, timeout,
            )
        if response.status >= 400:
            raise self._error_for(method, path, response)
        return response

    async def _http_request(
        self,
        method: str,
        path: str,
        query: Dict[str, Any],
        body: Optional[Dict[str, Any]],
        headers: Dict[str, str],
        timeout: Optional[float],
    ) -> RelayResponse:
        if self._http is None:
            self.open()
        assert self._http is not None
        try:
            raw = await self._http.request(
                method,
                f"{self.base_url}{path}",
                params={k: v for k, v in query.items() if v is not None} or None,
                json=body,
                headers=headers or None,
                timeout=timeout or self._request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise RelayApiError(
                f"relay: {method} {path} timed out", kind="retryable",
            ) from exc
        except Exception as exc:  # network-layer failure: worth another try
            raise RelayApiError(
                f"relay: network error: {exc}", kind="retryable",
            ) from exc
        parsed: Any = None
        try:
            parsed = raw.json()
        except Exception:
            parsed = None
        return RelayResponse(status=raw.status_code, body=parsed, text=raw.text)

    @staticmethod
    def _error_for(method: str, path: str, response: RelayResponse) -> RelayApiError:
        code: Optional[str] = None
        detail = ""
        details: Dict[str, Any] = {}
        body = response.body
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                detail = error.get("message") or ""
                if isinstance(error.get("details"), dict):
                    details = error["details"]
            detail = detail or body.get("message") or ""
        message = f"relay: {method} {path} failed with {response.status}"
        if detail:
            message = f"{message}: {detail}"
        return RelayApiError(
            message,
            kind=classify_status(response.status),
            status=response.status,
            code=code,
            details=details,
        )

    # -- endpoints ----------------------------------------------------------

    async def get_me(self) -> Dict[str, Any]:
        """``GET /v1/agents/me`` -> the agent profile.

        ``owner_user_id`` on that profile is what the default allowlist keys
        on, so this call is also the authorization bootstrap.
        """
        response = await self._request("GET", "/v1/agents/me")
        body = response.body or {}
        return body.get("agent", {}) if isinstance(body, dict) else {}

    async def poll_events(
        self,
        cursor: int,
        *,
        timeout_seconds: int = MAX_POLL_TIMEOUT_SECONDS,
        limit: Optional[int] = None,
    ) -> EventsPage:
        """``GET /v1/events`` long poll. Returns this page and its next cursor."""
        window = max(MIN_POLL_TIMEOUT_SECONDS, min(timeout_seconds, MAX_POLL_TIMEOUT_SECONDS))
        response = await self._request(
            "GET",
            "/v1/events",
            query={"cursor": cursor, "timeout": window, "limit": limit},
            timeout=window + POLL_READ_MARGIN_SECONDS,
        )
        body = response.body if isinstance(response.body, dict) else {}
        events = body.get("events")
        next_cursor = body.get("next_cursor")
        # A missing or malformed next_cursor holds position rather than
        # rewinding to zero, which would replay the whole retained log.
        if not isinstance(next_cursor, int) or isinstance(next_cursor, bool) or next_cursor < 0:
            next_cursor = cursor
        return EventsPage(
            events=events if isinstance(events, list) else [],
            next_cursor=next_cursor,
        )

    async def send_message(
        self,
        conversation_id: str,
        parts: List[Dict[str, Any]],
        *,
        idempotency_key: str,
        reply_to: Optional[Dict[str, Any]] = None,
        invocation_id: Optional[str] = None,
        timeout: Optional[float] = 30.0,
    ) -> Dict[str, Any]:
        """``POST /v1/messages`` -> one canonical message, committed once."""
        body: Dict[str, Any] = {"conversation_id": conversation_id, "parts": parts}
        if invocation_id:
            body["invocation_id"] = invocation_id
        if reply_to:
            body["reply_to"] = reply_to
        response = await self._request(
            "POST", "/v1/messages",
            body=body,
            headers={"idempotency-key": idempotency_key},
            timeout=timeout,
        )
        return response.body if isinstance(response.body, dict) else {}

    async def send_text(
        self,
        conversation_id: str,
        text: str,
        *,
        idempotency_key: str,
        reply_to: Optional[Dict[str, Any]] = None,
        invocation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self.send_message(
            conversation_id,
            [{"type": "text", "text": text}],
            idempotency_key=idempotency_key,
            reply_to=reply_to,
            invocation_id=invocation_id,
        )

    async def set_typing(
        self,
        conversation_id: str,
        started: bool,
        *,
        label: Optional[str] = None,
        invocation_id: Optional[str] = None,
        timeout: Optional[float] = 5.0,
    ) -> None:
        """``POST /v1/conversations/{id}/typing``. Ephemeral, never logged."""
        body: Dict[str, Any] = {"started": started}
        if label:
            body["label"] = label
        if invocation_id:
            body["invocation_id"] = invocation_id
        await self._request(
            "POST", f"/v1/conversations/{conversation_id}/typing",
            body=body, timeout=timeout,
        )

    async def set_responding(
        self,
        conversation_id: str,
        message_id: str,
        *,
        label: Optional[str] = None,
        invocation_id: Optional[str] = None,
    ) -> None:
        body: Dict[str, Any] = {"message_id": message_id}
        if label:
            body["label"] = label
        if invocation_id:
            body["invocation_id"] = invocation_id
        await self._request(
            "POST", f"/v1/conversations/{conversation_id}/responding", body=body,
        )

    async def mark_read(self, conversation_id: str, message_id: str) -> None:
        await self._request(
            "POST", f"/v1/conversations/{conversation_id}/read",
            body={"message_id": message_id},
        )


# ---------------------------------------------------------------------------
# Poll loop (port of poll-loop.ts)
# ---------------------------------------------------------------------------


def transient_delay_seconds(attempt: int, random_fn: Callable[[], float] = _random.random) -> float:
    """The backoff ladder from poll-loop.ts, in seconds.

    ``attempt`` is the count of consecutive transient failures, so the first
    retry is attempt 1. 500ms doubles per attempt, the exponent is capped at
    6, the result is clamped to 30s, and up to 250ms of jitter is added so a
    fleet of consumers does not retry in lockstep:
    1.0s, 2.0s, 4.0s, 8.0s, 16.0s, then 30.0s forever.
    """
    exponent = min(max(attempt, 0), TRANSIENT_MAX_EXPONENT)
    backoff_ms = min(TRANSIENT_MAX_DELAY_MS, TRANSIENT_BASE_DELAY_MS * (2 ** exponent))
    return (backoff_ms + int(random_fn() * TRANSIENT_JITTER_MS)) / 1000.0


def is_message_received(event: Dict[str, Any]) -> bool:
    if event.get("event_type") != "message.received":
        return False
    data = event.get("data")
    return isinstance(data, dict) and isinstance(data.get("message"), dict)


@dataclass
class InboundMessage:
    """One inbound ``message.received`` event, unpacked for the handler."""

    event: Dict[str, Any]
    event_id: str
    message: Dict[str, Any]
    conversation_id: str
    message_id: str
    sender_id: str
    sender_kind: str
    invocation_id: Optional[str] = None

    @property
    def is_group(self) -> bool:
        """A group turn is exactly one that carries an invocation id.

        The server rejects an agent reply or typing signal in a group without
        the pending invocation, and the first committed reply consumes it.
        """
        return bool(self.invocation_id)


def parse_inbound(event: Dict[str, Any]) -> Optional[InboundMessage]:
    if not is_message_received(event):
        return None
    data = event["data"]
    message = data["message"]
    sender = message.get("sender") or {}
    return InboundMessage(
        event=event,
        event_id=str(event.get("event_id") or ""),
        message=message,
        conversation_id=str(message.get("conversation_id") or ""),
        message_id=str(message.get("id") or ""),
        sender_id=str(sender.get("id") or ""),
        sender_kind=str(sender.get("kind") or ""),
        invocation_id=data.get("invocation_id") or None,
    )


async def run_poll_loop(
    *,
    client: RelayClient,
    get_cursor: Callable[[], int],
    set_cursor: Callable[[int], Any],
    dedupe: MemoryDedupe,
    on_message: Callable[[InboundMessage], Awaitable[None]],
    allow_sender: Optional[Callable[[str], bool]] = None,
    should_continue: Callable[[], bool] = lambda: True,
    timeout_seconds: int = MAX_POLL_TIMEOUT_SECONDS,
    limit: Optional[int] = None,
    log: Callable[[str], None] = logger.info,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = _random.random,
) -> None:
    """Receive loop for hosts that cannot expose a public webhook URL.

    Three ordering rules carry the delivery guarantee, and each one is load
    bearing:

    1. ``on_message`` runs before ``dedupe.record``. Recording on entry looks
       equivalent and silently drops messages: a handler that raises leaves
       the cursor unsaved, the server redelivers, and the event then looks
       like a duplicate.
    2. The cursor advances once, after every event on the page was handled. A
       failure mid-page replays the page rather than skipping its tail.
    3. Only ``retryable`` failures back off and retry. Terminal ones stop the
       loop, because retrying an identical request cannot fix them.
    """
    transient_attempts = 0

    while should_continue():
        try:
            page = await client.poll_events(
                get_cursor(), timeout_seconds=timeout_seconds, limit=limit,
            )
        except asyncio.CancelledError:
            raise
        except RelayApiError as error:
            if not should_continue():
                return
            if error.terminal:
                _log_terminal(error, log)
                raise
            transient_attempts += 1
            delay = transient_delay_seconds(transient_attempts, random_fn)
            log(f"[relay] transient poll error (attempt {transient_attempts}): {error}")
            await sleep(delay)
            continue

        transient_attempts = 0

        for event in page.events:
            if not should_continue():
                return
            inbound = parse_inbound(event)
            if inbound is None:
                continue
            # Echo-loop guard: never react to this agent's own messages.
            if inbound.sender_kind == "agent":
                continue
            if (
                allow_sender is not None
                and inbound.sender_kind == "user"
                and not allow_sender(inbound.sender_id)
            ):
                # A dropped sender is a decided outcome, not a failure, so it
                # is recorded: redelivering it would re-run the same drop.
                dedupe.record(inbound.event_id)
                continue
            if dedupe.has(inbound.event_id):
                continue
            await on_message(inbound)
            dedupe.record(inbound.event_id)

        result = set_cursor(page.next_cursor)
        if asyncio.iscoroutine(result):
            await result


def _log_terminal(error: RelayApiError, log: Callable[[str], None]) -> None:
    """Say what an operator has to do, per failure, in plain words."""
    if is_consumer_takeover(error):
        log(
            "[relay] another consumer took this Agent Token (409 "
            "terminated_by_other_consumer). Relay allows one long-poll consumer "
            "per token, and restarting will not win the slot back. Stop the "
            "other process, or issue this Hermes its own agent and token."
        )
    elif error.status == 410:
        log(
            "[relay] cursor expired (410): it is behind the seven-day retention "
            "ceiling. Reconcile from conversation history. Do not reset the "
            "cursor to zero."
        )
    elif error.status == 422:
        highest = error.details.get("highest_delivered_cursor")
        where = "error.details.highest_delivered_cursor" if highest is None else f"cursor {highest}"
        log(
            "[relay] cursor ahead of the delivered ledger (422): reconcile "
            f"history over REST, then resume from {where}."
        )
    elif error.kind == "auth":
        log("[relay] Relay rejected the Agent Token (401). Check RELAY_AGENT_TOKEN.")
    elif error.kind == "conflict":
        log(
            f"[relay] long poll conflict: {error}. Webhooks and long polling are "
            "mutually exclusive per Agent Token: disable the webhook endpoint to "
            "poll."
        )
    else:
        log(f"[relay] poll stopped: {error}")


# ---------------------------------------------------------------------------
# Message rendering helpers
# ---------------------------------------------------------------------------


def render_text(message: Dict[str, Any]) -> str:
    """Flatten a canonical message's ordered parts into one string.

    ``fallback_text`` is the server's own plain rendering and covers the case
    where every part was media.
    """
    chunks: List[str] = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "link") and part.get("text"):
            chunks.append(str(part["text"]))
        elif kind == "link" and part.get("url"):
            chunks.append(str(part["url"]))
        elif kind == "data" and part.get("data") is not None:
            chunks.append(json.dumps(part["data"], ensure_ascii=False))
    rendered = "\n".join(chunks).strip()
    return rendered or str(message.get("fallback_text") or "").strip()
