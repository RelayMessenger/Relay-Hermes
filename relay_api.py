"""Current Relay v1 REST and WebSocket transport for Hermes."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import random as _random
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple, Union
from urllib.parse import quote, urlsplit, urlunsplit

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
    WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover
    websocket_connect = None  # type: ignore[assignment]
    ConnectionClosed = None  # type: ignore[assignment,misc]
    InvalidStatus = None  # type: ignore[assignment,misc]
    WEBSOCKETS_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.relayapp.im"
RELAY_API_VERSION = "v1"
RELAY_WEBHOOK_VERSION = "2026-08-30"
RELAY_OPENAPI_COMMIT = "a25111520f7fc92c25ecd945d1dfc9afa9f60a1f"
RELAY_OPENAPI_SHA256 = (
    "9f3e662a13cd0e6b16a52fba4b53c75fe5817d134dcf152e00b054699c37839c"
)
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
MAX_TEXT_PART_UNITS = 10_000
MAX_PARTS_PER_MESSAGE = 100
TRANSIENT_BASE_DELAY_MS = 500
TRANSIENT_MAX_DELAY_MS = 30_000
HEARTBEAT_PING_INTERVAL_SECONDS = 30.0
HEARTBEAT_PONG_TIMEOUT_SECONDS = 60.0
# Cloudflare answers this exact text frame with {"type":"pong"} without
# waking the Durable Object, and reads the last one's time for liveness.
HEARTBEAT_PING_FRAME = json.dumps({"type": "ping"}, separators=(",", ":"))
WEBSOCKET_WEBHOOK_CONFIGURED_CLOSE_CODE = 4410
_SEQUENCE_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")
_DNS_LABEL_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_WEBHOOK_EVENT_TYPES = {
    "message.sent",
    "message.received",
    "message.failed",
    "message.read",
    "message.delivered",
    "reaction.added",
    "reaction.removed",
    "participant.added",
    "participant.removed",
    "chat.created",
    "chat.group_name_updated",
    "chat.group_icon_updated",
    "chat.typing_indicator.started",
    "chat.typing_indicator.stopped",
    "contact.added",
    "contact.removed",
}
_DISCONNECT_REASONS = {
    "revoked",
    "heartbeat_timeout",
    "restart",
    "webhook_configured",
}
_WEBSOCKET_ERROR_CODES = {
    "invalid_frame",
    "ack_out_of_range",
    "stale_connection",
    "ack_failed",
    "delivery_failed",
    "full_sync_required",
    "full_sync_mismatch",
}


class RelayApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: Optional[int] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.code = code
        self.details = details or {}
        # The server's own error.message, without this client's prefix.
        self.detail = detail

    @property
    def terminal(self) -> bool:
        return self.kind != "retryable"

    @property
    def retryable(self) -> bool:
        return self.kind == "retryable"


class RelayWebSocketError(Exception):
    """A server-supplied WebSocket error frame."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        fatal: bool,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.fatal = fatal
        self.retryable = retryable

    @property
    def terminal(self) -> bool:
        # fatal means the current connection cannot continue. retryable is
        # the authority for whether a fresh direct connection may resume.
        return not self.retryable


class RelayWebSocketDisconnect(Exception):
    """A server-supplied disconnect frame."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Relay WebSocket disconnected: {reason}")
        self.reason = reason

    @property
    def terminal(self) -> bool:
        return self.reason not in ("heartbeat_timeout", "restart")


class RelayWebSocketClosed(Exception):
    """The connection ended without a terminal Relay frame."""


class RelayWebSocketProtocolError(RuntimeError):
    """A frame violated the canonical Relay WebSocket schema."""


class RelayFullSyncError(RuntimeError):
    """The adapter cannot safely reconcile a required Relay FULL sync."""


class RelayWebhookConfiguredError(RelayApiError):
    """The Agent has a Webhook subscription, so Relay rejected its socket."""

    def __init__(
        self,
        message: str = (
            "This Agent delivers by webhook; delete its webhook subscription "
            "to use the WebSocket."
        ),
        *,
        trace_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message,
            kind="conflict",
            status=409,
            code="webhook_configured",
            details=details,
        )
        self.trace_id = trace_id


# Relay answers a reused Idempotency-Key whose stored content hash differs
# from the new body with HTTP 409 and this message (Relay-Server
# server/src/messaging.ts:743 and :847 at contract snapshot 9990699,
# `throw new ApiError(409, 1005, "Idempotency key was already used for
# different message content.")`). The contract documents the status only:
# openapi.yaml:712 "The request conflicts with current Chat, membership, or
# idempotency state", and its ErrorCode is a bare integer (openapi.yaml:2476),
# where 1005 is the server's generic rejection code shared with "This Chat has
# no active recipient." (messaging.ts:905). The message text is therefore the
# only field that names the idempotency case, and it is matched by prefix.
IDEMPOTENCY_REUSE_MESSAGE = "Idempotency key was already used"


def is_idempotency_reuse(error: "RelayApiError") -> bool:
    """Did Relay refuse this send because its key already names a message?

    That message was committed by the first send under the same key, so the
    logical send succeeded; the caller must never send again.
    """
    return (
        error.status == 409
        and error.detail.lower().startswith(IDEMPOTENCY_REUSE_MESSAGE.lower())
    )


def classify_status(status: int) -> str:
    if status == 401:
        return "auth"
    if status in (403, 409):
        return "conflict"
    if status in (408, 429) or status >= 500:
        return "retryable"
    return "rejected"


def _upgrade_error(error: Any) -> RelayApiError:
    response = getattr(error, "response", None)
    status = int(getattr(response, "status_code", 0) or 0)
    raw = bytes(getattr(response, "body", b"") or b"")
    parsed: Any = None
    message = ""
    trace_id: Optional[str] = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = raw.decode("utf-8", errors="replace")
    if isinstance(parsed, dict):
        trace = parsed.get("trace_id")
        if isinstance(trace, str):
            trace_id = trace
        body_error = parsed.get("error")
        if (
            isinstance(body_error, dict)
            and isinstance(body_error.get("message"), str)
        ):
            message = body_error["message"]
        elif isinstance(parsed.get("message"), str):
            message = parsed["message"]
    if status == 409:
        return RelayWebhookConfiguredError(
            message or (
                "This Agent delivers by webhook; delete its webhook "
                "subscription to use the WebSocket."
            ),
            trace_id=trace_id,
            details={"body": parsed},
        )
    fallback = (
        f"Relay WebSocket upgrade failed with HTTP {status}"
        if status
        else "Relay WebSocket upgrade failed before receiving an HTTP status"
    )
    return RelayApiError(
        message or fallback,
        kind=classify_status(status) if status else "retryable",
        status=status or None,
        details={"body": parsed},
    )


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.strip().lower().strip("[]").rstrip(".")
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return normalized == "localhost" or normalized.endswith(".localhost")


def _normalize_hostname(hostname: str, candidate: str) -> str:
    normalized = hostname.lower().rstrip(".")
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        pass
    try:
        normalized = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"relay: invalid base URL {candidate!r}") from exc
    if (
        not normalized
        or len(normalized) > 253
        or any(
            _DNS_LABEL_PATTERN.fullmatch(label) is None
            for label in normalized.split(".")
        )
    ):
        raise ValueError(f"relay: invalid base URL {candidate!r}")
    return normalized


def normalize_base_url(raw: Optional[str]) -> str:
    candidate = (raw or "").strip() or DEFAULT_BASE_URL
    if any(character.isspace() for character in candidate):
        raise ValueError(f"relay: invalid base URL {candidate!r}")
    parts = urlsplit(candidate)
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"relay: invalid base URL {candidate!r}")
    if parts.username or parts.password:
        raise ValueError("relay: base URL must not contain credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("relay: base URL must be an origin")
    if parts.scheme != "https" and not (
        parts.scheme == "http" and _is_loopback_host(parts.hostname)
    ):
        raise ValueError(
            "relay: base URL must use HTTPS (HTTP is allowed only on loopback)"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"relay: invalid base URL {candidate!r}") from exc
    normalized_host = _normalize_hostname(parts.hostname, candidate)
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    default_port = (
        parts.scheme == "https" and port == 443
    ) or (
        parts.scheme == "http" and port == 80
    )
    authority = (
        normalized_host
        if port is None or default_port
        else f"{normalized_host}:{port}"
    )
    return f"{parts.scheme}://{authority}"


def websocket_url(base_url: str) -> str:
    """Derive the credential-free Relay WebSocket URL from an API origin."""

    normalized = normalize_base_url(base_url)
    parts = urlsplit(normalized)
    return urlunsplit((
        "wss" if parts.scheme == "https" else "ws",
        parts.netloc,
        "/v1/websocket",
        "",
        "",
    ))


def reply_idempotency_key(
    event_id: str,
    ordinal: int = 0,
) -> str:
    """Derive one retry-stable key from a Relay event and send ordinal."""

    return f"reply-{event_id}-{ordinal}"


@dataclass
class RelayResponse:
    status: int
    body: Any = None
    text: str = ""


Transport = Callable[..., Awaitable[RelayResponse]]


def _full_sync_page(
    body: Dict[str, Any],
    *,
    collection: str,
    item_id: str,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    items = body.get(collection)
    cursor = body.get("next_cursor")
    if not isinstance(items, list):
        raise RelayFullSyncError(
            f"Relay FULL sync response is missing a {collection} array."
        )
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise RelayFullSyncError(
            f"Relay FULL sync returned an invalid {collection} cursor."
        )
    validated: List[Dict[str, Any]] = []
    for item in items:
        identifier = item.get(item_id) if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(identifier, str)
            or _UUID_PATTERN.fullmatch(identifier) is None
        ):
            raise RelayFullSyncError(
                f"Relay FULL sync returned an invalid {collection} item."
            )
        validated.append(item)
    return validated, cursor


class RelayClient:
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
        # Attachment URLs are capabilities and can point at storage on another
        # origin. Keep them on a client that never carries the Agent Token.
        self._transfer_http: Optional[Any] = None

    async def __aenter__(self) -> "RelayClient":
        self.open()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def open(self) -> None:
        if not HTTPX_AVAILABLE:
            raise RuntimeError("relay: httpx is required")
        if self._transport is None and self._http is None:
            self._http = httpx.AsyncClient(
                headers={"authorization": f"Bearer {self._token}"},
                timeout=self._request_timeout,
            )
        if self._transfer_http is None:
            self._transfer_http = httpx.AsyncClient(timeout=120.0)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._transfer_http is not None:
            await self._transfer_http.aclose()
            self._transfer_http = None

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
                method=method,
                path=path,
                query=query or {},
                body=body,
                headers=headers or {},
                timeout=timeout,
            )
        else:
            response = await self._http_request(
                method, path, query or {}, body, headers or {}, timeout
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
        except Exception as exc:
            raise RelayApiError(
                f"relay: network error: {exc}", kind="retryable"
            ) from exc
        try:
            parsed = raw.json()
        except Exception:
            parsed = None
        return RelayResponse(raw.status_code, parsed, raw.text)

    @staticmethod
    def _error_for(
        method: str, path: str, response: RelayResponse
    ) -> RelayApiError:
        code: Optional[str] = None
        detail = ""
        details: Dict[str, Any] = {}
        if isinstance(response.body, dict):
            error = response.body.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                detail = str(error.get("message") or "")
                if isinstance(error.get("details"), dict):
                    details = error["details"]
            detail = detail or str(response.body.get("message") or "")
        message = f"relay: {method} {path} failed with {response.status}"
        if detail:
            message += f": {detail}"
        return RelayApiError(
            message,
            kind=classify_status(response.status),
            status=response.status,
            code=code,
            details=details,
            detail=detail,
        )

    async def list_chats(
        self,
        *,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        response = await self._request(
            "GET",
            "/v1/chats",
            query={"cursor": cursor, "limit": limit},
        )
        return response.body if isinstance(response.body, dict) else {}

    async def list_messages(
        self,
        chat_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        response = await self._request(
            "GET",
            f"/v1/chats/{quote(chat_id, safe='')}/messages",
            query={"cursor": cursor, "limit": limit},
        )
        return response.body if isinstance(response.body, dict) else {}

    async def fetch_full_snapshot(self) -> List[Dict[str, Any]]:
        """Fetch every visible Chat and Message with defensive pagination.

        The returned list is deliberately plain JSON so the SQLite layer can
        canonicalize and atomically replace its durable snapshot.
        """

        chats: List[Dict[str, Any]] = []
        chat_ids: set[str] = set()
        chat_cursors: set[str] = set()
        cursor: Optional[str] = None
        while True:
            page = await self.list_chats(cursor=cursor, limit=100)
            page_chats, next_cursor = _full_sync_page(
                page,
                collection="chats",
                item_id="id",
            )
            for chat in page_chats:
                chat_id = str(chat["id"])
                if chat_id in chat_ids:
                    raise RelayFullSyncError(
                        f"Relay FULL sync returned Chat {chat_id} more than once."
                    )
                chat_ids.add(chat_id)
                chats.append({"chat": chat, "messages": []})
            if next_cursor is None:
                break
            if next_cursor in chat_cursors:
                raise RelayFullSyncError(
                    "Relay FULL sync repeated a Chats pagination cursor."
                )
            chat_cursors.add(next_cursor)
            cursor = next_cursor

        message_ids: set[str] = set()
        for entry in chats:
            chat = entry["chat"]
            chat_id = str(chat["id"])
            message_cursors: set[str] = set()
            cursor = None
            while True:
                page = await self.list_messages(
                    chat_id,
                    cursor=cursor,
                    limit=100,
                )
                messages, next_cursor = _full_sync_page(
                    page,
                    collection="messages",
                    item_id="id",
                )
                for message in messages:
                    message_id = str(message["id"])
                    if message.get("chat_id") != chat_id:
                        raise RelayFullSyncError(
                            f"Relay FULL sync Message {message_id} belongs to "
                            f"{message.get('chat_id')!r}, not Chat {chat_id}."
                        )
                    if message_id in message_ids:
                        raise RelayFullSyncError(
                            f"Relay FULL sync returned Message {message_id} "
                            "more than once."
                        )
                    message_ids.add(message_id)
                    entry["messages"].append(message)
                if next_cursor is None:
                    break
                if next_cursor in message_cursors:
                    raise RelayFullSyncError(
                        f"Relay FULL sync repeated a Messages cursor for "
                        f"Chat {chat_id}."
                    )
                message_cursors.add(next_cursor)
                cursor = next_cursor
        return chats

    async def start_typing(self, chat_id: str) -> None:
        await self._request("POST", f"/v1/chats/{quote(chat_id, safe='')}/typing")

    async def stop_typing(self, chat_id: str) -> None:
        await self._request("DELETE", f"/v1/chats/{quote(chat_id, safe='')}/typing")

    async def send_reaction(self, message_id: str, emoji: str, *, operation: str) -> None:
        await self._request(
            "POST", f"/v1/messages/{quote(message_id, safe='')}/reactions",
            body={"operation": operation, "type": "custom", "custom_emoji": emoji},
        )

    async def send_message(
        self,
        chat_id: str,
        parts: List[Dict[str, Any]],
        *,
        idempotency_key: str,
        reply_to: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = 30.0,
    ) -> Dict[str, Any]:
        idempotency_key = (idempotency_key or "").strip()
        if not idempotency_key or len(idempotency_key) > 255:
            raise ValueError(
                "relay: Idempotency-Key must contain 1 to 255 characters"
            )
        if not parts:
            raise ValueError("relay: a Message must contain at least one part")
        message: Dict[str, Any] = {"parts": parts}
        if reply_to:
            message["reply_to"] = reply_to
        response = await self._request(
            "POST",
            f"/v1/chats/{quote(chat_id, safe='')}/messages",
            body={"message": message},
            headers={"Idempotency-Key": idempotency_key},
            timeout=timeout,
        )
        return response.body if isinstance(response.body, dict) else {}

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        idempotency_key: str,
        reply_to: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await self.send_message(
            chat_id,
            [{"type": "text", "value": text}],
            idempotency_key=idempotency_key,
            reply_to=reply_to,
        )

    async def mark_read(self, chat_id: str) -> None:
        await self._request(
            "POST", f"/v1/chats/{quote(chat_id, safe='')}/read"
        )

    async def request_upload(
        self,
        *,
        filename: str,
        content_type: str,
        size_bytes: int,
    ) -> Dict[str, Any]:
        response = await self._request(
            "POST",
            "/v1/attachments",
            body={
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
            },
        )
        return response.body if isinstance(response.body, dict) else {}

    async def upload_attachment(
        self,
        *,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> str:
        allocation = await self.request_upload(
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
        )
        attachment_id = allocation.get("attachment_id")
        upload_url = allocation.get("upload_url")
        required_headers = allocation.get("required_headers")
        if not isinstance(attachment_id, str) or not isinstance(upload_url, str):
            raise RelayApiError(
                "relay: upload allocation was incomplete", kind="rejected"
            )
        if self._transfer_http is None:
            self.open()
        if self._transfer_http is None:
            raise RuntimeError("relay: attachment upload requires an HTTP client")
        response = await self._transfer_http.put(
            upload_url,
            content=data,
            headers=required_headers if isinstance(required_headers, dict) else {},
            timeout=120.0,
        )
        if response.status_code >= 300:
            raise RelayApiError(
                f"relay: attachment PUT failed with {response.status_code}",
                kind=classify_status(response.status_code),
                status=response.status_code,
            )
        return attachment_id


def transient_delay_seconds(
    attempt: int, random_fn: Callable[[], float] = _random.random
) -> float:
    ceiling = min(
        TRANSIENT_MAX_DELAY_MS,
        TRANSIENT_BASE_DELAY_MS * (2 ** max(0, attempt - 1)),
    )
    return random_fn() * ceiling / 1000.0


class DurableInbox(Protocol):
    def accept(self, sequence: str, event: Dict[str, Any]) -> bool: ...


FullSyncHandler = Callable[[str, str], Awaitable[None]]


async def consume_websocket(
    socket: Any,
    *,
    inbox: DurableInbox,
    on_accepted: Callable[[], Any] = lambda: None,
    on_ready: Callable[[], Any] = lambda: None,
    on_full_sync: Optional[FullSyncHandler] = None,
    ping_interval_seconds: float = HEARTBEAT_PING_INTERVAL_SECONDS,
    pong_timeout_seconds: float = HEARTBEAT_PONG_TIMEOUT_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Commit before cumulative ACK; transport ACK never processes or Reads."""

    last_pong_at = monotonic()

    async def receive() -> None:
        nonlocal last_pong_at
        ready = False
        accepted_through: Optional[int] = None
        full_sync_through: Optional[int] = None
        full_sync_pending = False
        async for raw in socket:
            if not isinstance(raw, str):
                raise RelayWebSocketProtocolError(
                    "Relay WebSocket received a non-text frame"
                )
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise RelayWebSocketProtocolError(
                    "Relay WebSocket received invalid JSON"
                ) from exc
            frame_type = frame.get("type") if isinstance(frame, dict) else None
            if frame_type == "ready":
                checkpoint = frame.get("acked_through")
                requires_full_sync = frame.get("full_sync_required")
                required_through = frame.get("full_sync_through")
                if (
                    ready
                    or set(frame) != {
                        "type",
                        "connection_id",
                        "acked_through",
                        "full_sync_required",
                        "full_sync_through",
                        "heartbeat_interval_ms",
                        "max_in_flight",
                    }
                    or not isinstance(checkpoint, str)
                    or _SEQUENCE_PATTERN.fullmatch(checkpoint) is None
                    or not isinstance(requires_full_sync, bool)
                    or (
                        requires_full_sync
                        and (
                            not isinstance(required_through, str)
                            or _SEQUENCE_PATTERN.fullmatch(required_through) is None
                        )
                    )
                    or (not requires_full_sync and required_through is not None)
                    or not isinstance(frame.get("connection_id"), str)
                    or _UUID_PATTERN.fullmatch(frame["connection_id"]) is None
                    or isinstance(frame.get("heartbeat_interval_ms"), bool)
                    or not isinstance(frame.get("heartbeat_interval_ms"), int)
                    or frame["heartbeat_interval_ms"] < 1
                    or isinstance(frame.get("max_in_flight"), bool)
                    or not isinstance(frame.get("max_in_flight"), int)
                    or frame["max_in_flight"] < 1
                ):
                    raise RelayWebSocketProtocolError(
                        "Relay WebSocket received an invalid ready frame"
                    )
                accepted_through = int(checkpoint)
                full_sync_pending = requires_full_sync
                full_sync_through = (
                    int(required_through) if requires_full_sync else None
                )
                ready = True
                result = on_ready()
                if asyncio.iscoroutine(result):
                    await result
                continue
            if frame_type == "disconnect":
                reason = frame.get("reason")
                if set(frame) != {"type", "reason"} or reason not in _DISCONNECT_REASONS:
                    raise RelayWebSocketProtocolError(
                        "Relay WebSocket received an invalid disconnect frame"
                    )
                raise RelayWebSocketDisconnect(reason)
            if frame_type == "ping":
                if (
                    set(frame) != {"type", "sent_at"}
                    or not ready
                    or not isinstance(frame.get("sent_at"), str)
                ):
                    raise RelayWebSocketProtocolError(
                        "Relay WebSocket received an invalid ping frame"
                    )
                await socket.send(json.dumps(
                    {"type": "pong"},
                    separators=(",", ":"),
                ))
                continue
            if frame_type == "pong":
                # The answer to our own heartbeat. Cloudflare replies before the
                # Durable Object wakes, so a pong may arrive before "ready".
                if set(frame) != {"type"}:
                    raise RelayWebSocketProtocolError(
                        "Relay WebSocket received an invalid pong frame"
                    )
                last_pong_at = monotonic()
                continue
            if frame_type == "error":
                code = frame.get("code")
                message = frame.get("message")
                fatal = frame.get("fatal")
                retryable = frame.get("retryable")
                if (
                    set(frame) != {
                        "type",
                        "code",
                        "message",
                        "fatal",
                        "retryable",
                    }
                    or not isinstance(code, str)
                    or code not in _WEBSOCKET_ERROR_CODES
                    or not isinstance(message, str)
                    or not isinstance(fatal, bool)
                    or not isinstance(retryable, bool)
                ):
                    raise RelayWebSocketProtocolError(
                        "Relay WebSocket received an invalid error frame"
                    )
                raise RelayWebSocketError(
                    message,
                    code=code,
                    fatal=fatal,
                    retryable=retryable,
                )
            if frame_type == "full_sync":
                through = frame.get("through_sequence")
                reason = frame.get("reason")
                if (
                    set(frame) != {"type", "through_sequence", "reason"}
                    or not ready
                    or not full_sync_pending
                    or not isinstance(through, str)
                    or _SEQUENCE_PATTERN.fullmatch(through) is None
                    or int(through) != full_sync_through
                    or reason != "checkpoint_outside_retention"
                ):
                    raise RelayWebSocketProtocolError(
                        "Relay WebSocket received an invalid FULL sync frame"
                    )
                if on_full_sync is None:
                    raise RelayFullSyncError(
                        "Relay requires a FULL sync, but this consumer has no "
                        "safe snapshot reconciler."
                    )
                await on_full_sync(through, reason)
                await socket.send(json.dumps({
                    "type": "full_sync_complete",
                    "through_sequence": through,
                }, separators=(",", ":")))
                accepted_through = int(through)
                full_sync_pending = False
                full_sync_through = None
                continue
            if full_sync_pending and frame_type == "event":
                raise RelayWebSocketProtocolError(
                    "Relay WebSocket received an event while FULL sync was pending"
                )
            if (
                frame_type != "event"
                or set(frame) != {"type", "sequence", "event"}
                or not ready
            ):
                raise RelayWebSocketProtocolError(
                    "Relay WebSocket received an invalid frame"
                )
            sequence = frame.get("sequence")
            event = frame.get("event")
            if (
                not isinstance(sequence, str)
                or _SEQUENCE_PATTERN.fullmatch(sequence) is None
            ):
                raise RelayWebSocketProtocolError(
                    "Relay WebSocket received an invalid sequence"
                )
            if (
                not isinstance(event, dict)
                or event.get("api_version") != RELAY_API_VERSION
                or event.get("webhook_version") != RELAY_WEBHOOK_VERSION
                or event.get("event_type") not in _WEBHOOK_EVENT_TYPES
                or not isinstance(event.get("event_id"), str)
                or _UUID_PATTERN.fullmatch(event["event_id"]) is None
                or not isinstance(event.get("created_at"), str)
                or not isinstance(event.get("trace_id"), str)
                or not isinstance(event.get("agent_id"), str)
                or _UUID_PATTERN.fullmatch(event["agent_id"]) is None
                or not isinstance(event.get("data"), dict)
            ):
                raise RelayWebSocketProtocolError(
                    "Relay WebSocket received an invalid event"
                )
            sequence_number = int(sequence)
            if accepted_through is None or sequence_number != accepted_through + 1:
                raise RelayWebSocketProtocolError(
                    "Relay WebSocket sequence is not contiguous"
                )
            inbox.accept(sequence, event)
            accepted_through = sequence_number
            await socket.send(json.dumps({
                "type": "ack",
                "through_sequence": sequence,
            }, separators=(",", ":")))
            result = on_accepted()
            if asyncio.iscoroutine(result):
                await result
        raise RelayWebSocketClosed("Relay WebSocket closed")

    async def heartbeat() -> None:
        # One heartbeat, ours. The websockets library's protocol pings are off
        # (ping_interval=None) because the server never sees them.
        while True:
            await sleep(ping_interval_seconds)
            if monotonic() - last_pong_at >= pong_timeout_seconds:
                raise RelayWebSocketDisconnect("heartbeat_timeout")
            await socket.send(HEARTBEAT_PING_FRAME)

    receive_task = asyncio.ensure_future(receive())
    heartbeat_task = asyncio.ensure_future(heartbeat())
    try:
        done, _pending = await asyncio.wait(
            (receive_task, heartbeat_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        receive_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(
            receive_task,
            heartbeat_task,
            return_exceptions=True,
        )
    # A frame-level verdict outranks the heartbeat's when both land together.
    if receive_task in done:
        return receive_task.result()
    return heartbeat_task.result()


async def run_websocket_loop(
    *,
    client: RelayClient,
    inbox: DurableInbox,
    on_accepted: Callable[[], Any] = lambda: None,
    on_ready: Callable[[], Any] = lambda: None,
    on_full_sync: Optional[FullSyncHandler] = None,
    should_continue: Callable[[], bool] = lambda: True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    random_fn: Callable[[], float] = _random.random,
    connect_factory: Optional[Callable[..., Any]] = None,
    log: Callable[[str], None] = logger.warning,
) -> None:
    """Reconnect with full jitter. Processing is deliberately a separate loop."""

    connector = connect_factory or websocket_connect
    if connector is None:
        raise RuntimeError("relay: websockets is required")
    url = websocket_url(client.base_url)
    attempt = 0
    while should_continue():
        try:
            async with connector(
                url,
                additional_headers={
                    "Authorization": f"Bearer {client._token}",  # noqa: SLF001
                },
                ping_interval=None,
            ) as socket:
                def mark_ready() -> Any:
                    nonlocal attempt
                    attempt = 0
                    return on_ready()

                await consume_websocket(
                    socket,
                    inbox=inbox,
                    on_accepted=on_accepted,
                    on_ready=mark_ready,
                    on_full_sync=on_full_sync,
                )
        except asyncio.CancelledError:
            raise
        except RelayApiError as error:
            if error.terminal:
                raise
            attempt += 1
            delay = transient_delay_seconds(attempt, random_fn)
            log(f"WebSocket reconnect in {delay:.2f}s: {error}")
            await sleep(delay)
        except RelayWebSocketError as error:
            if error.terminal:
                raise
            attempt += 1
            delay = transient_delay_seconds(attempt, random_fn)
            log(f"WebSocket reconnect in {delay:.2f}s: {error}")
            await sleep(delay)
        except RelayWebSocketDisconnect as error:
            if error.terminal:
                raise
            attempt += 1
            delay = transient_delay_seconds(attempt, random_fn)
            log(f"WebSocket reconnect in {delay:.2f}s: {error}")
            await sleep(delay)
        except RelayWebSocketProtocolError:
            raise
        except RelayFullSyncError:
            raise
        except Exception as error:
            if not should_continue():
                return
            if InvalidStatus is not None and isinstance(error, InvalidStatus):
                upgrade_error = _upgrade_error(error)
                if upgrade_error.terminal:
                    raise upgrade_error from error
                attempt += 1
                delay = transient_delay_seconds(attempt, random_fn)
                log(f"WebSocket reconnect in {delay:.2f}s: {upgrade_error}")
                await sleep(delay)
                continue
            if (
                ConnectionClosed is not None
                and isinstance(error, ConnectionClosed)
            ):
                code = (
                    getattr(getattr(error, "rcvd", None), "code", None)
                    or getattr(error, "code", None)
                )
                reason = (
                    getattr(getattr(error, "rcvd", None), "reason", None)
                    or getattr(error, "reason", None)
                    or "server policy changed"
                )
                if (
                    isinstance(code, int)
                    and 4400 <= code <= 4499
                    and code != 4408
                ):
                    if code == WEBSOCKET_WEBHOOK_CONFIGURED_CLOSE_CODE:
                        raise RelayWebhookConfiguredError(
                            (
                                "This Agent now delivers by webhook; remove "
                                "every webhook subscription before reconnecting."
                            ),
                            details={
                                "close_code": code,
                                "close_reason": reason,
                            },
                        ) from error
                    raise RelayWebSocketDisconnect(
                        f"server_policy_{code}: {reason}"
                    ) from error
            attempt += 1
            delay = transient_delay_seconds(attempt, random_fn)
            log(f"WebSocket reconnect in {delay:.2f}s: {error}")
            await sleep(delay)


@dataclass
class InboundRelayMessage:
    event: Dict[str, Any]
    event_id: str
    message: Dict[str, Any]
    chat_id: str
    message_id: str
    sender_contact_id: str
    sender_contact_kind: str
    sender_contact_name: str
    agent_handle: str
    is_group: bool
    created_at: Optional[str]


def parse_inbound(event: Dict[str, Any]) -> Optional[InboundRelayMessage]:
    if event.get("event_type") != "message.received":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or data.get("direction") != "inbound":
        return None
    chat = data.get("chat")
    sender = data.get("sender_handle")
    parts = data.get("parts")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    if not isinstance(parts, list):
        return None
    owner = chat.get("owner_handle")
    return InboundRelayMessage(
        event=event,
        event_id=str(event.get("event_id") or ""),
        message=data,
        chat_id=str(chat.get("id") or ""),
        message_id=str(data.get("id") or ""),
        sender_contact_id=str(sender.get("id") or ""),
        sender_contact_kind=str(sender.get("kind") or ""),
        sender_contact_name=str(
            sender.get("display_name") or sender.get("handle") or ""
        ),
        agent_handle=(
            str(owner.get("handle") or "") if isinstance(owner, dict) else ""
        ),
        is_group=bool(chat.get("is_group")),
        created_at=(
            str(data.get("sent_at"))
            if isinstance(data.get("sent_at"), str)
            else str(event.get("created_at") or "") or None
        ),
    )


def mentions_agent(message: Dict[str, Any], *, handle: str) -> bool:
    wanted = handle.lstrip("@").lower()
    if not wanted:
        return False
    for part in message.get("parts") or []:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        named = part.get("mention")
        if isinstance(named, str) and named.lstrip("@").lower() == wanted:
            return True
    return False


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_paragraphs(text: str) -> List[str]:
    blocks: List[str] = []
    current: List[str] = []
    in_fence = False
    for line in text.strip().splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
        if not line.strip() and not in_fence:
            block = "\n".join(current).strip()
            if block:
                blocks.append(block)
            current = []
        else:
            current.append(line)
    block = "\n".join(current).strip()
    if block:
        blocks.append(block)
    return blocks


def render_text(message: Dict[str, Any]) -> str:
    values: List[str] = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        # A tap arrives as ordinary text carrying the label. The agent's own
        # buttons part reads as nothing, as on the server; its question is the
        # text beside it.
        if part.get("type") in ("text", "link") and isinstance(part.get("value"), str):
            values.append(part["value"])
    return "\n".join(values).strip()


# Buttons under a message, the way the Relay SDK's ``splitButtons`` lifts them
# out of an agent's words (Relay-SDK packages/sdk/src/buttons.ts, ported here
# like the rest of this file). The model ends its answer with a fenced block
# tagged ``buttons`` holding the part's ``items`` array; the block becomes the
# buttons part and the words stay the text. Limits are the server's
# (Discord's button limits): 1 to 5 items, label 1 to 80, url at most 2,048.
BUTTONS_FENCE = "buttons"
BUTTONS_MAX_ITEMS = 5
BUTTON_LABEL_MAX_LENGTH = 80
BUTTON_URL_MAX_LENGTH = 2_048

# The same words every Relay runtime carries (the SDK's BUTTONS_GUIDANCE).
BUTTONS_GUIDANCE = " ".join([
    "Send buttons when your message ends with a question the person can answer by picking one of 2 to 5 short options you already know: yes or no, choosing between things you named, picking a next step, or a multiple-choice question in a quiz. Each label is a complete answer, so a tap replaces typing. Put the question in text beside the buttons.",
    "Send one button when there is one thing to do next. A url button is for a task the person completes on a web page: pay, sign in, connect an account, open their booking or order, track a package. Its label names the action, not the site. A plain button confirms one step: Start, Done, Continue. Do not ask \"ready?\" when a single button does the job.",
    "A link is for something the person will look at or read: an article, a listing, a video, a place, a product page, a support article. Send it as a link on its own, so it draws as a card with the page's title and image; never paste a bare URL into your words, and send one link per message. When the page is where the person does something, send a url button; when the page is the thing you are showing them, send a link.",
    "Do not send buttons when the answer is open-ended, when your options are not the full set of likely answers, or when you are not asking anything and there is nothing to do. One question or one action per message; never a menu of things you can do, and never as decoration.",
    "If you would otherwise write \"reply 1, 2 or 3\" or list choices for the person to type, send buttons instead. If the person asks for buttons, send them.",
    "A tap comes back to you as an ordinary message whose text is the label. Labels are at most 80 characters.",
    "Buttons disappear once tapped.",
])

# How the model puts buttons under its answer (the SDK's BUTTONS_BLOCK_INSTRUCTION).
BUTTONS_BLOCK_INSTRUCTION = (
    "To put buttons under your answer, end it with a fenced code block tagged `"
    + BUTTONS_FENCE
    + "` holding a JSON array of 1 to 5 items, each {\"label\": \"...\"} or "
    "{\"label\": \"...\", \"url\": \"https://...\"}. "
    "The block is removed from the text and drawn as buttons."
)

# How the model sends a link (the SDK's LINK_LINE_INSTRUCTION): the URL alone
# on its own line, the way a person pastes one into Messages. The line leaves
# the words and goes out as a ``link`` part in its own Message, which the
# server requires and the app draws as a card.
LINK_LINE_INSTRUCTION = (
    "To send a link, put its URL alone on its own line. That line leaves your "
    "text and is sent as its own Message, in order with your words, and drawn "
    "as a card with the page's title and image."
)

LINK_URL_MAX_LENGTH = 2_048

_STANDALONE_LINK_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def standalone_link(line: str) -> Optional[str]:
    """The URL a line carries when it is nothing but one absolute HTTP(S) URL.

    A URL inside a sentence stays words, as does one past the server's limit
    or one without a host.
    """
    value = line.strip()
    if not _STANDALONE_LINK_RE.match(value) or len(value) > LINK_URL_MAX_LENGTH:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return None
    return value


def bubble_part(chunk: str) -> Dict[str, Any]:
    """One bubble as a part: a link when the bubble is only a URL, else text."""
    link = standalone_link(chunk)
    if link is not None:
        return {"type": "link", "value": link}
    return {"type": "text", "value": chunk}

_BUTTONS_FENCE_RE = re.compile(
    r"(^|\n)[ \t]*```[ \t]*" + BUTTONS_FENCE + r"[ \t]*\r?\n([\s\S]*?)\r?\n[ \t]*```[ \t]*(?=\r?\n|$)"
)


def _button_item(value: Any, index: int) -> Union[Dict[str, Any], str]:
    if not isinstance(value, dict):
        return f"item {index + 1} is not an object"
    unknown = [key for key in value if key not in ("label", "url")]
    if unknown:
        return f"item {index + 1} has unknown field {unknown[0]}"
    label = value.get("label")
    if not isinstance(label, str) or not label:
        return f"item {index + 1} needs a label"
    if utf16_len(label) > BUTTON_LABEL_MAX_LENGTH:
        return f"item {index + 1} label is over {BUTTON_LABEL_MAX_LENGTH} characters"
    if "url" not in value:
        return {"label": label}
    url = value.get("url")
    if not isinstance(url, str) or len(url) > BUTTON_URL_MAX_LENGTH:
        return f"item {index + 1} url is not a string of at most {BUTTON_URL_MAX_LENGTH} characters"
    if not re.match(r"^https?://[^\s]+$", url):
        return f"item {index + 1} url is not an http(s) URL"
    return {"url": url, "label": label}


def buttons_part(parsed: Any) -> Union[Dict[str, Any], str]:
    """A ``buttons`` part from a decoded items array or whole part, or why not."""
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
        items = parsed["items"]
    else:
        return "the buttons block must be a JSON array of items"
    if not items:
        return "the buttons block has no items"
    if len(items) > BUTTONS_MAX_ITEMS:
        return f"the buttons block has {len(items)} items; the most is {BUTTONS_MAX_ITEMS}"
    result: List[Dict[str, Any]] = []
    for index, value in enumerate(items):
        item = _button_item(value, index)
        if isinstance(item, str):
            return item
        result.append(item)
    return {"type": "buttons", "items": result}


def split_buttons(answer: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Lift the first buttons block out of an answer.

    Returns ``(text, buttons_part, error)``. A block that cannot be read leaves
    the answer untouched and names the reason, so the person still gets the
    words and the operator sees why.
    """
    match = _BUTTONS_FENCE_RE.search(answer)
    if match is None:
        return answer, None, None
    try:
        parsed = json.loads(match.group(2))
    except ValueError:
        return answer, None, "the buttons block is not valid JSON"
    part = buttons_part(parsed)
    if isinstance(part, str):
        return answer, None, part
    start = match.start() + len(match.group(1))
    before = answer[:start].rstrip()
    after = answer[match.end():].lstrip()
    text = f"{before}\n\n{after}" if before and after else (before or after)
    return text, part, None
