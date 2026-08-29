"""Current Relay v1 REST and WebSocket transport for Hermes."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import random as _random
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol
from urllib.parse import quote, urlsplit

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.exceptions import ConnectionClosed
    WEBSOCKETS_AVAILABLE = True
except ImportError:  # pragma: no cover
    websocket_connect = None  # type: ignore[assignment]
    ConnectionClosed = None  # type: ignore[assignment,misc]
    WEBSOCKETS_AVAILABLE = False

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.relayapp.im"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
MAX_TEXT_PART_UNITS = 10_000
MAX_PARTS_PER_MESSAGE = 100
TRANSIENT_BASE_DELAY_MS = 500
TRANSIENT_MAX_DELAY_MS = 30_000
_SEQUENCE_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_WEBHOOK_EVENT_TYPES = {
    "message.sent",
    "message.received",
    "message.read",
    "message.delivered",
    "reaction.added",
    "reaction.removed",
    "participant.added",
    "participant.removed",
    "chat.created",
    "chat.group_name_updated",
    "chat.group_icon_updated",
}
_DISCONNECT_REASONS = {
    "disabled",
    "replaced",
    "revoked",
    "heartbeat_timeout",
    "restart",
}
_WEBSOCKET_ERROR_CODES = {
    "invalid_frame",
    "ack_out_of_range",
    "stale_connection",
    "ack_failed",
    "delivery_failed",
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
        # the authority for whether a fresh ticket/connection may resume.
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
    """A frame or connection ticket violated the canonical Relay schema."""


def classify_status(status: int) -> str:
    if status == 401:
        return "auth"
    if status in (403, 409):
        return "conflict"
    if status in (408, 429) or status >= 500:
        return "retryable"
    return "rejected"


def _is_loopback_host(hostname: str) -> bool:
    normalized = hostname.strip().lower().strip("[]")
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return normalized == "localhost" or normalized.endswith(".localhost")


def normalize_base_url(raw: Optional[str]) -> str:
    candidate = (raw or "").strip() or DEFAULT_BASE_URL
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
    return f"{parts.scheme}://{parts.netloc}"


def reply_idempotency_key(
    event_id: str,
    ordinal: int = 0,
    content: Optional[Dict[str, Any]] = None,
) -> str:
    """Derive one stable key from the event and logical request body."""

    if content is None:
        return f"reply-{event_id}-{ordinal}"
    canonical = json.dumps(content, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"reply-{event_id}-{ordinal}-{digest}"


@dataclass
class RelayResponse:
    status: int
    body: Any = None
    text: str = ""


Transport = Callable[..., Awaitable[RelayResponse]]


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
        )

    async def get_websocket_settings(self) -> Dict[str, Any]:
        response = await self._request("GET", "/v1/websocket")
        return response.body if isinstance(response.body, dict) else {}

    async def update_websocket(self, enabled: bool) -> Dict[str, Any]:
        response = await self._request(
            "PUT", "/v1/websocket", body={"enabled": enabled}
        )
        return response.body if isinstance(response.body, dict) else {}

    async def create_websocket_connection(self) -> Dict[str, Any]:
        response = await self._request(
            "POST", "/v1/websocket-connections", body={}
        )
        return response.body if isinstance(response.body, dict) else {}

    async def send_message(
        self,
        chat_id: str,
        parts: List[Dict[str, Any]],
        *,
        idempotency_key: str,
        reply_to: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = 30.0,
    ) -> Dict[str, Any]:
        message: Dict[str, Any] = {"parts": parts}
        if reply_to:
            message["reply_to"] = reply_to
        response = await self._request(
            "POST",
            f"/v1/chats/{quote(chat_id, safe='')}/messages",
            body={"message": message},
            headers={"idempotency-key": idempotency_key},
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

    async def get_chat(self, chat_id: str) -> Dict[str, Any]:
        response = await self._request(
            "GET", f"/v1/chats/{quote(chat_id, safe='')}"
        )
        return response.body if isinstance(response.body, dict) else {}

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

    async def send_voice_memo(
        self,
        chat_id: str,
        attachment_id: str,
    ) -> Dict[str, Any]:
        response = await self._request(
            "POST",
            f"/v1/chats/{quote(chat_id, safe='')}/voicememo",
            body={"attachment_id": attachment_id},
        )
        return response.body if isinstance(response.body, dict) else {}


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


async def consume_websocket(
    socket: Any,
    *,
    inbox: DurableInbox,
    on_accepted: Callable[[], Any] = lambda: None,
    on_ready: Callable[[], Any] = lambda: None,
) -> None:
    """Consume one connection, commit before cumulative ACK, and never process."""

    ready = False
    accepted_through: Optional[int] = None
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
            if (
                ready
                or set(frame) != {
                    "type",
                    "connection_id",
                    "acked_through",
                    "heartbeat_interval_ms",
                    "max_in_flight",
                }
                or not isinstance(checkpoint, str)
                or _SEQUENCE_PATTERN.fullmatch(checkpoint) is None
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
            or event.get("api_version") != "v1"
            or not isinstance(event.get("webhook_version"), str)
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


async def run_websocket_loop(
    *,
    client: RelayClient,
    inbox: DurableInbox,
    on_accepted: Callable[[], Any] = lambda: None,
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
    attempt = 0
    while should_continue():
        try:
            ticket = await client.create_websocket_connection()
            url = ticket.get("url")
            expires_at = ticket.get("expires_at")
            protocol = ticket.get("subprotocol")
            parsed_url = urlsplit(url) if isinstance(url, str) else None
            if (
                set(ticket) != {"url", "expires_at", "subprotocol"}
                or parsed_url is None
                or parsed_url.scheme not in ("ws", "wss")
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
                or not isinstance(expires_at, str)
                or protocol != "relay.v1.json"
            ):
                raise RelayWebSocketProtocolError(
                    "Relay returned an invalid WebSocket ticket"
                )
            async with connector(url, subprotocols=[protocol]) as socket:
                def mark_ready() -> None:
                    nonlocal attempt
                    attempt = 0

                await consume_websocket(
                    socket,
                    inbox=inbox,
                    on_accepted=on_accepted,
                    on_ready=mark_ready,
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
        except Exception as error:
            if not should_continue():
                return
            if (
                ConnectionClosed is not None
                and isinstance(error, ConnectionClosed)
                and (
                    getattr(error, "code", None)
                    or getattr(getattr(error, "rcvd", None), "code", None)
                ) == 4409
            ):
                raise RelayWebSocketDisconnect("replaced") from error
            attempt += 1
            delay = transient_delay_seconds(attempt, random_fn)
            log(f"WebSocket reconnect in {delay:.2f}s: {error}")
            await sleep(delay)


@dataclass
class InboundMessage:
    event: Dict[str, Any]
    event_id: str
    message: Dict[str, Any]
    chat_id: str
    message_id: str
    sender_id: str
    sender_kind: str
    sender_name: str
    agent_handle: str
    is_group: bool
    created_at: Optional[str]


def parse_inbound(event: Dict[str, Any]) -> Optional[InboundMessage]:
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
    return InboundMessage(
        event=event,
        event_id=str(event.get("event_id") or ""),
        message=data,
        chat_id=str(chat.get("id") or ""),
        message_id=str(data.get("id") or ""),
        sender_id=str(sender.get("id") or ""),
        sender_kind=str(sender.get("kind") or ""),
        sender_name=str(sender.get("display_name") or sender.get("handle") or ""),
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
        if part.get("type") in ("text", "link") and isinstance(part.get("value"), str):
            values.append(part["value"])
    return "\n".join(values).strip()
