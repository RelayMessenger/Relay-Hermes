from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from hermes_relay_plugin.state import RelayInbox
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from hermes_relay_plugin.relay_api import (
    RelayClient,
    RelayFullSyncError,
    RelayResponse,
    RELAY_API_VERSION,
    RELAY_WEBHOOK_VERSION,
    RelayWebhookConfiguredError,
    RelayWebSocketClosed,
    RelayWebSocketDisconnect,
    RelayWebSocketError,
    RelayWebSocketProtocolError,
    classify_status,
    consume_websocket,
    mentions_agent,
    normalize_base_url,
    parse_inbound,
    render_text,
    reply_idempotency_key,
    run_websocket_loop,
    split_paragraphs,
    transient_delay_seconds,
    utf16_len,
    websocket_url,
)

TOKEN = "relay-test-token"
CHAT_ID = "01993d50-ef7b-7b37-886b-23fd80c7ec10"
MESSAGE_ID = "01993d50-ef7b-7b37-886b-23fd80c7ec11"
EVENT_ID = "01993d50-ef7b-7b37-886b-23fd80c7ec12"
CHAT_ID_2 = "01993d50-ef7b-7b37-886b-23fd80c7ec20"
MESSAGE_ID_2 = "01993d50-ef7b-7b37-886b-23fd80c7ec21"


class FakeTransport:
    def __init__(self, responses: List[RelayResponse]) -> None:
        self.responses = responses
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> RelayResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def event(*, group: bool = False, mention: bool = False) -> Dict[str, Any]:
    text: Dict[str, Any] = {"type": "text", "value": "@helper hello"}
    if mention:
        text.update({"mention": "helper", "mention_range": [0, 7]})
    return {
        "api_version": "v1",
        "webhook_version": "2026-08-30",
        "event_id": EVENT_ID,
        "event_type": "message.received",
        "created_at": "2026-08-29T00:00:00Z",
        "trace_id": "trace-test",
        "agent_id": "01993d50-ef7b-7b37-886b-23fd80c7ec13",
        "data": {
            "chat": {
                "id": CHAT_ID,
                "is_group": group,
                "owner_handle": {
                    "id": "01993d50-ef7b-7b37-886b-23fd80c7ec14",
                    "handle": "helper",
                    "joined_at": "2026-08-29T00:00:00Z",
                    "kind": "agent",
                    "is_me": True,
                },
            },
            "id": MESSAGE_ID,
            "direction": "inbound",
            "sender_handle": {
                "id": "01993d50-ef7b-7b37-886b-23fd80c7ec15",
                "handle": "advait",
                "joined_at": "2026-08-29T00:00:00Z",
                "kind": "user",
                "display_name": "Advait",
            },
            "parts": [text],
            "sent_at": "2026-08-29T00:00:00Z",
        },
    }


def ready(
    acked_through: str = "0",
    full_sync_through: str | None = None,
) -> Dict[str, Any]:
    return {
        "type": "ready",
        "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
        "acked_through": acked_through,
        "full_sync_required": full_sync_through is not None,
        "full_sync_through": full_sync_through,
        "heartbeat_interval_ms": 30_000,
        "max_in_flight": 64,
    }


def test_current_event_parsing_and_mentions():
    inbound = parse_inbound(event(group=True, mention=True))
    assert inbound is not None
    assert inbound.chat_id == CHAT_ID
    assert inbound.message_id == MESSAGE_ID
    assert inbound.is_group is True
    assert inbound.agent_handle == "helper"
    assert inbound.sender_name == "Advait"
    assert mentions_agent(inbound.message, handle=inbound.agent_handle)


def test_relay_contract_versions_and_product_paths_stay_current():
    assert RELAY_API_VERSION == "v1"
    assert RELAY_WEBHOOK_VERSION == "2026-08-30"
    root = Path(__file__).resolve().parents[1]
    for name in ("relay_api.py", "adapter.py", "README.md", "plugin.yaml"):
        assert "/v3" not in (root / name).read_text(encoding="utf-8"), name


def test_visible_at_text_is_not_a_structured_mention():
    inbound = parse_inbound(event(group=True, mention=False))
    assert inbound is not None
    assert not mentions_agent(inbound.message, handle="helper")


def test_render_text_uses_value_and_link_parts():
    message = event()["data"]
    message["parts"] = [
        {"type": "text", "value": "one"},
        {"type": "media", "url": "https://files.example/photo"},
        {"type": "link", "value": "https://example.com"},
    ]
    assert render_text(message) == "one\nhttps://example.com"


def test_send_message_uses_chat_route_and_current_body():
    transport = FakeTransport([
        RelayResponse(202, {"chat_id": CHAT_ID, "message": {"id": MESSAGE_ID}})
    ])
    client = RelayClient(TOKEN, transport=transport)
    result = asyncio.run(client.send_text(
        CHAT_ID,
        "hello",
        idempotency_key="reply-key",
        reply_to={"message_id": MESSAGE_ID},
    ))
    assert result["message"]["id"] == MESSAGE_ID
    call = transport.calls[0]
    assert call["path"] == f"/v1/chats/{CHAT_ID}/messages"
    assert call["headers"] == {"idempotency-key": "reply-key"}
    assert call["body"] == {
        "message": {
            "parts": [{"type": "text", "value": "hello"}],
            "reply_to": {"message_id": MESSAGE_ID},
        }
    }


def test_read_has_no_body_and_voice_has_dedicated_route():
    transport = FakeTransport([
        RelayResponse(204),
        RelayResponse(202, {"voice_memo": {"id": MESSAGE_ID}}),
    ])
    client = RelayClient(TOKEN, transport=transport)
    asyncio.run(client.mark_read(CHAT_ID))
    result = asyncio.run(client.send_voice_memo(
        CHAT_ID,
        "01993d50-ef7b-7b37-886b-23fd80c7ec16",
    ))
    assert result["voice_memo"]["id"] == MESSAGE_ID
    assert transport.calls[0]["path"] == f"/v1/chats/{CHAT_ID}/read"
    assert transport.calls[0]["body"] is None
    assert transport.calls[1]["path"] == f"/v1/chats/{CHAT_ID}/voicememo"
    assert transport.calls[1]["body"] == {
        "attachment_id": "01993d50-ef7b-7b37-886b-23fd80c7ec16",
    }
    assert transport.calls[1]["headers"] == {}


def test_attachment_allocates_with_json_then_puts_raw_bytes():
    attachment_id = "01993d50-ef7b-7b37-886b-23fd80c7ec16"
    transport = FakeTransport([
        RelayResponse(200, {
            "attachment_id": attachment_id,
            "upload_url": "https://upload.example/one-use",
            "download_url": "https://download.example/file",
            "http_method": "PUT",
            "expires_at": "2026-08-29T00:15:00Z",
            "required_headers": {"content-type": "image/png"},
        }),
    ])

    class FakeHttp:
        def __init__(self):
            self.calls = []

        async def put(self, url, *, content, headers, timeout):
            self.calls.append({
                "url": url,
                "content": content,
                "headers": headers,
                "timeout": timeout,
            })
            return type("Response", (), {"status_code": 200})()

    client = RelayClient(TOKEN, transport=transport)
    raw = FakeHttp()
    client._transfer_http = raw
    result = asyncio.run(client.upload_attachment(
        filename="photo.png",
        content_type="image/png",
        data=b"PNG",
    ))
    assert result == attachment_id
    assert transport.calls[0]["path"] == "/v1/attachments"
    assert transport.calls[0]["body"] == {
        "filename": "photo.png",
        "content_type": "image/png",
        "size_bytes": 3,
    }
    assert raw.calls == [{
        "url": "https://upload.example/one-use",
        "content": b"PNG",
        "headers": {"content-type": "image/png"},
        "timeout": 120.0,
    }]


def test_client_has_no_websocket_mode_or_enable_api():
    client = RelayClient(TOKEN, transport=FakeTransport([]))
    assert not hasattr(client, "get_websocket_settings")
    assert not hasattr(client, "update_websocket")


def test_full_snapshot_fetches_every_chat_and_message_page():
    chat_one = {"id": CHAT_ID, "is_group": False}
    chat_two = {"id": CHAT_ID_2, "is_group": True}
    message_one = {
        "id": MESSAGE_ID,
        "chat_id": CHAT_ID,
        "is_from_me": False,
    }
    message_two = {
        "id": MESSAGE_ID_2,
        "chat_id": CHAT_ID,
        "is_from_me": True,
    }
    transport = FakeTransport([
        RelayResponse(200, {"chats": [chat_one], "next_cursor": "chats-2"}),
        RelayResponse(200, {"chats": [chat_two], "next_cursor": None}),
        RelayResponse(200, {
            "messages": [message_one],
            "next_cursor": "messages-2",
        }),
        RelayResponse(200, {
            "messages": [message_two],
            "next_cursor": None,
        }),
        RelayResponse(200, {"messages": [], "next_cursor": None}),
    ])
    client = RelayClient(TOKEN, transport=transport)

    assert asyncio.run(client.fetch_full_snapshot()) == [
        {"chat": chat_one, "messages": [message_one, message_two]},
        {"chat": chat_two, "messages": []},
    ]
    assert [(call["path"], call["query"]) for call in transport.calls] == [
        ("/v1/chats", {"cursor": None, "limit": 100}),
        ("/v1/chats", {"cursor": "chats-2", "limit": 100}),
        (
            f"/v1/chats/{CHAT_ID}/messages",
            {"cursor": None, "limit": 100},
        ),
        (
            f"/v1/chats/{CHAT_ID}/messages",
            {"cursor": "messages-2", "limit": 100},
        ),
        (
            f"/v1/chats/{CHAT_ID_2}/messages",
            {"cursor": None, "limit": 100},
        ),
    ]


def test_full_snapshot_fails_closed_on_repeated_cursor():
    transport = FakeTransport([
        RelayResponse(200, {"chats": [], "next_cursor": "same"}),
        RelayResponse(200, {"chats": [], "next_cursor": "same"}),
    ])
    client = RelayClient(TOKEN, transport=transport)
    with pytest.raises(RelayFullSyncError, match="repeated"):
        asyncio.run(client.fetch_full_snapshot())


class OrderedInbox:
    def __init__(self, order: List[str]) -> None:
        self.order = order
        self.events: List[Dict[str, Any]] = []

    def accept(self, sequence: str, payload: Dict[str, Any]) -> bool:
        self.order.append(f"commit:{sequence}")
        self.events.append(payload)
        return True


class FakeSocket:
    def __init__(self, frames: List[Dict[str, Any]], order: List[str]) -> None:
        self.frames = [json.dumps(frame) for frame in frames]
        self.sent: List[Dict[str, Any]] = []
        self.order = order

    def __aiter__(self):
        async def iterator():
            for frame in self.frames:
                yield frame
        return iterator()

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        label = frame["type"]
        if "through_sequence" in frame:
            self.order.append(f"{label}:{frame['through_sequence']}")
        else:
            self.order.append(label)
        self.sent.append(frame)


def test_websocket_commits_before_cumulative_ack():
    order: List[str] = []
    socket = FakeSocket([
        ready("40"),
        {"type": "event", "sequence": "41", "event": event()},
    ], order)
    inbox = OrderedInbox(order)
    with pytest.raises(RelayWebSocketClosed):
        asyncio.run(consume_websocket(socket, inbox=inbox))
    assert order == ["commit:41", "ack:41"]
    assert inbox.events == [event()]
    assert socket.sent == [{"type": "ack", "through_sequence": "41"}]


def test_websocket_refuses_to_ack_over_a_sequence_gap():
    order: List[str] = []
    socket = FakeSocket([
        ready("40"),
        {"type": "event", "sequence": "42", "event": event()},
    ], order)
    with pytest.raises(RuntimeError, match="not contiguous"):
        asyncio.run(consume_websocket(socket, inbox=OrderedInbox(order)))
    assert order == []


@pytest.mark.parametrize("value", ["00", "01", "-1", "1.0"])
def test_websocket_rejects_noncanonical_decimal_sequences(value):
    order: List[str] = []
    socket = FakeSocket([
        ready(),
        {"type": "event", "sequence": value, "event": event()},
    ], order)
    with pytest.raises(RuntimeError, match="invalid sequence"):
        asyncio.run(consume_websocket(socket, inbox=OrderedInbox(order)))
    assert order == []


def test_websocket_honors_error_and_disconnect_frames():
    ready_frame = ready()
    retryable = FakeSocket([
        ready_frame,
        {
            "type": "error",
            "code": "delivery_failed",
            "message": "try again",
            "fatal": True,
            "retryable": True,
        },
    ], [])
    with pytest.raises(RelayWebSocketError) as raised:
        asyncio.run(consume_websocket(retryable, inbox=OrderedInbox([])))
    assert raised.value.code == "delivery_failed"
    assert raised.value.terminal is False

    for reason in (
        "revoked",
        "heartbeat_timeout",
        "restart",
        "webhook_configured",
    ):
        disconnected = FakeSocket([
            ready_frame,
            {"type": "disconnect", "reason": reason},
        ], [])
        with pytest.raises(RelayWebSocketDisconnect) as raised:
            asyncio.run(consume_websocket(disconnected, inbox=OrderedInbox([])))
        assert raised.value.reason == reason
        assert raised.value.terminal is (
            reason not in ("heartbeat_timeout", "restart")
        )

    unknown_error = FakeSocket([
        ready_frame,
        {
            "type": "error",
            "code": "unknown",
            "message": "not in Relay v1",
            "fatal": True,
            "retryable": False,
        },
    ], [])
    with pytest.raises(RelayWebSocketProtocolError, match="invalid error"):
        asyncio.run(consume_websocket(
            unknown_error,
            inbox=OrderedInbox([]),
        ))


def test_websocket_requires_ready_exact_keys_and_complete_envelopes():
    with pytest.raises(RuntimeError, match="invalid frame"):
        asyncio.run(consume_websocket(
            FakeSocket([
                {"type": "event", "sequence": "1", "event": event()},
            ], []),
            inbox=OrderedInbox([]),
        ))

    invalid_ready = ready()
    invalid_ready["heartbeat_interval_ms"] = True
    with pytest.raises(RuntimeError, match="invalid ready"):
        asyncio.run(consume_websocket(
            FakeSocket([invalid_ready], []),
            inbox=OrderedInbox([]),
        ))

    extra = event()
    frames = [
        ready(),
        {"type": "event", "sequence": "1", "event": extra, "unexpected": True},
    ]
    with pytest.raises(RuntimeError, match="invalid frame"):
        asyncio.run(consume_websocket(
            FakeSocket(frames, []),
            inbox=OrderedInbox([]),
        ))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_version", "v3"),
        ("webhook_version", "2026-08-31"),
    ],
)
def test_websocket_rejects_noncurrent_event_contract(field, value):
    invalid = event()
    invalid[field] = value
    order: List[str] = []
    socket = FakeSocket([
        ready(),
        {"type": "event", "sequence": "1", "event": invalid},
    ], order)

    with pytest.raises(RelayWebSocketProtocolError, match="invalid event"):
        asyncio.run(consume_websocket(
            socket,
            inbox=OrderedInbox(order),
        ))
    assert order == []
    assert socket.sent == []


def test_websocket_replies_to_application_ping_without_moving_checkpoint():
    order: List[str] = []
    socket = FakeSocket([
        ready("40"),
        {"type": "ping", "sent_at": "2026-08-29T00:00:30Z"},
        {"type": "event", "sequence": "41", "event": event()},
    ], order)
    with pytest.raises(RelayWebSocketClosed):
        asyncio.run(consume_websocket(
            socket,
            inbox=OrderedInbox(order),
        ))
    assert order == ["pong", "commit:41", "ack:41"]
    assert socket.sent == [
        {"type": "pong"},
        {"type": "ack", "through_sequence": "41"},
    ]


class FakeConnectionContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_exc):
        return None


def test_websocket_loop_reconnects_with_direct_token_then_stops_revoked():
    ready_frame = ready()
    sockets = [
        FakeSocket([
            ready_frame,
            {"type": "disconnect", "reason": "heartbeat_timeout"},
        ], []),
        FakeSocket([
            ready_frame,
            {"type": "disconnect", "reason": "restart"},
        ], []),
        FakeSocket([
            ready_frame,
            {"type": "disconnect", "reason": "revoked"},
        ], []),
    ]

    client = RelayClient(
        TOKEN,
        "https://relay.test",
        transport=FakeTransport([]),
    )
    connections = []
    sleeps = []

    def connect(
        url,
        *,
        additional_headers,
        ping_interval,
        ping_timeout,
    ):
        connections.append((
            url,
            additional_headers,
            ping_interval,
            ping_timeout,
        ))
        return FakeConnectionContext(sockets[len(connections) - 1])

    async def sleep(delay):
        sleeps.append(delay)

    with pytest.raises(RelayWebSocketDisconnect) as raised:
        asyncio.run(run_websocket_loop(
            client=client,
            inbox=OrderedInbox([]),
            connect_factory=connect,
            sleep=sleep,
            random_fn=lambda: 1.0,
        ))

    assert raised.value.reason == "revoked"
    assert [entry[0] for entry in connections] == [
        "wss://relay.test/v1/websocket",
        "wss://relay.test/v1/websocket",
        "wss://relay.test/v1/websocket",
    ]
    assert [entry[1] for entry in connections] == [
        {"Authorization": f"Bearer {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}"},
    ]
    assert all("?" not in entry[0] for entry in connections)
    assert [(entry[2], entry[3]) for entry in connections] == [
        (30.0, 60.0),
        (30.0, 60.0),
        (30.0, 60.0),
    ]
    assert sleeps == [0.5, 0.5]


def test_websocket_loop_stops_on_protocol_violation_without_reconnecting():
    socket = FakeSocket([
        {"type": "event", "sequence": "1", "event": event()},
    ], [])

    client = RelayClient(
        TOKEN,
        "https://relay.test",
        transport=FakeTransport([]),
    )
    connections = []

    def connect(
        url,
        *,
        additional_headers,
        ping_interval,
        ping_timeout,
    ):
        connections.append((
            url,
            additional_headers,
            ping_interval,
            ping_timeout,
        ))
        return FakeConnectionContext(socket)

    with pytest.raises(RelayWebSocketProtocolError):
        asyncio.run(run_websocket_loop(
            client=client,
            inbox=OrderedInbox([]),
            connect_factory=connect,
        ))
    assert connections == [(
        "wss://relay.test/v1/websocket",
        {"Authorization": f"Bearer {TOKEN}"},
        30.0,
        60.0,
    )]


def test_websocket_loop_surfaces_webhook_configured_http_409_once():
    body = json.dumps({
        "error": {
            "message": (
                "This Agent delivers by webhook; delete its webhook "
                "subscription to use the WebSocket."
            ),
        },
        "trace_id": "trace-webhook-conflict",
    }).encode()
    connections = []

    def connect(url, **options):
        connections.append((url, options))
        raise InvalidStatus(Response(
            409,
            "Conflict",
            headers=Headers(),
            body=body,
        ))

    client = RelayClient(
        TOKEN,
        "https://relay.test",
        transport=FakeTransport([]),
    )
    with pytest.raises(RelayWebhookConfiguredError) as raised:
        asyncio.run(run_websocket_loop(
            client=client,
            inbox=OrderedInbox([]),
            connect_factory=connect,
        ))
    assert raised.value.status == 409
    assert raised.value.trace_id == "trace-webhook-conflict"
    assert "delete its webhook subscription" in str(raised.value)
    assert len(connections) == 1


def test_websocket_loop_treats_unknown_server_policy_close_as_terminal():
    class PolicyCloseSocket(FakeSocket):
        def __aiter__(self):
            async def iterator():
                yield json.dumps(ready())
                raise ConnectionClosedError(
                    Close(4499, "webhook delivery is now configured"),
                    None,
                    None,
                )
            return iterator()

    def connect(_url, **_options):
        return FakeConnectionContext(PolicyCloseSocket([], []))

    client = RelayClient(
        TOKEN,
        "https://relay.test",
        transport=FakeTransport([]),
    )
    with pytest.raises(RelayWebSocketDisconnect) as raised:
        asyncio.run(run_websocket_loop(
            client=client,
            inbox=OrderedInbox([]),
            connect_factory=connect,
        ))
    assert raised.value.terminal is True
    assert "4499" in raised.value.reason
    assert "webhook delivery is now configured" in raised.value.reason


def test_websocket_loop_maps_dedicated_webhook_close_to_clear_error():
    class WebhookCloseSocket(FakeSocket):
        def __aiter__(self):
            async def iterator():
                yield json.dumps(ready())
                raise ConnectionClosedError(
                    Close(4410, "webhook delivery is now configured"),
                    None,
                    None,
                )
            return iterator()

    def connect(_url, **_options):
        return FakeConnectionContext(WebhookCloseSocket([], []))

    client = RelayClient(
        TOKEN,
        "https://relay.test",
        transport=FakeTransport([]),
    )
    with pytest.raises(RelayWebhookConfiguredError) as raised:
        asyncio.run(run_websocket_loop(
            client=client,
            inbox=OrderedInbox([]),
            connect_factory=connect,
        ))
    assert raised.value.status == 409
    assert raised.value.details["close_code"] == 4410
    assert "remove every webhook subscription" in str(raised.value)


def test_websocket_replay_is_deduplicated_but_acknowledged_again(tmp_path):
    frames = [
        ready(),
        {"type": "event", "sequence": "1", "event": event()},
    ]
    inbox = RelayInbox(tmp_path / "inbox.sqlite3").open()
    first = FakeSocket(frames, [])
    replay = FakeSocket(frames, [])
    with pytest.raises(RelayWebSocketClosed):
        asyncio.run(consume_websocket(first, inbox=inbox))
    with pytest.raises(RelayWebSocketClosed):
        asyncio.run(consume_websocket(replay, inbox=inbox))
    assert inbox.event_count() == 1
    assert first.sent == [{"type": "ack", "through_sequence": "1"}]
    assert replay.sent == [{"type": "ack", "through_sequence": "1"}]
    inbox.close()


def test_full_sync_commits_before_completion_and_events_ack_afterward(tmp_path):
    order: List[str] = []
    socket = FakeSocket([
        ready("7", "42"),
        {
            "type": "full_sync",
            "through_sequence": "42",
            "reason": "checkpoint_outside_retention",
        },
        {"type": "event", "sequence": "43", "event": event()},
    ], order)
    class TrackingInbox(RelayInbox):
        def accept(self, sequence, payload):
            result = super().accept(sequence, payload)
            order.append(f"commit:{sequence}")
            return result

    inbox = TrackingInbox(tmp_path / "inbox.sqlite3").open()
    chat = {
        "id": CHAT_ID,
        "is_group": False,
    }
    message = {
        "id": MESSAGE_ID,
        "chat_id": CHAT_ID,
        "is_from_me": False,
    }

    async def full_sync(through: str, reason: str) -> None:
        assert reason == "checkpoint_outside_retention"
        inbox.replace_full_snapshot(
            through,
            [{"chat": chat, "messages": [message]}],
        )
        order.append(f"snapshot:{through}")

    with pytest.raises(RelayWebSocketClosed):
        asyncio.run(consume_websocket(
            socket,
            inbox=inbox,
            on_full_sync=full_sync,
        ))

    assert order == [
        "snapshot:42",
        "full_sync_complete:42",
        "commit:43",
        "ack:43",
    ]
    assert socket.sent == [
        {"type": "full_sync_complete", "through_sequence": "42"},
        {"type": "ack", "through_sequence": "43"},
    ]
    assert inbox.full_sync_state()["through_sequence"] == "42"
    inbox.close()


def test_full_sync_fails_closed_without_a_safe_reconciler():
    socket = FakeSocket([
        ready("0", "8"),
        {
            "type": "full_sync",
            "through_sequence": "8",
            "reason": "checkpoint_outside_retention",
        },
    ], [])
    with pytest.raises(RelayFullSyncError, match="no safe snapshot"):
        asyncio.run(consume_websocket(
            socket,
            inbox=OrderedInbox([]),
        ))
    assert socket.sent == []


def test_websocket_rejects_event_while_full_sync_is_pending():
    socket = FakeSocket([
        ready("0", "8"),
        {"type": "event", "sequence": "1", "event": event()},
    ], [])
    with pytest.raises(
        RelayWebSocketProtocolError,
        match="while FULL sync was pending",
    ):
        asyncio.run(consume_websocket(
            socket,
            inbox=OrderedInbox([]),
            on_full_sync=lambda *_args: None,
        ))
    assert socket.sent == []


def test_base_url_idempotency_and_errors():
    assert normalize_base_url(None) == "https://api.relayapp.im"
    assert normalize_base_url("http://localhost:8790") == "http://localhost:8790"
    assert websocket_url("https://api.relayapp.im") == (
        "wss://api.relayapp.im/v1/websocket"
    )
    assert websocket_url("http://localhost:8790") == (
        "ws://localhost:8790/v1/websocket"
    )
    with pytest.raises(ValueError):
        normalize_base_url("http://api.relayapp.im")
    assert reply_idempotency_key(EVENT_ID, 2) == f"reply-{EVENT_ID}-2"
    first = reply_idempotency_key(EVENT_ID, 0, {"message": {"value": "one"}})
    assert first == reply_idempotency_key(
        EVENT_ID, 0, {"message": {"value": "one"}}
    )
    assert first != reply_idempotency_key(
        EVENT_ID, 0, {"message": {"value": "two"}}
    )
    assert classify_status(401) == "auth"
    assert classify_status(429) == "retryable"
    assert classify_status(400) == "rejected"
    assert transient_delay_seconds(1, lambda: 1.0) == 0.5
    assert transient_delay_seconds(2, lambda: 1.0) == 1.0


def test_text_helpers_use_utf16_units_and_preserve_fences():
    assert utf16_len("a😀") == 3
    assert split_paragraphs("one\n\n```\na\n\nb\n```\n\ntwo") == [
        "one",
        "```\na\n\nb\n```",
        "two",
    ]
