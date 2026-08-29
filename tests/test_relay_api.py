from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

import pytest
from hermes_relay_plugin.state import RelayInbox

from hermes_relay_plugin.relay_api import (
    RelayClient,
    RelayResponse,
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
)

TOKEN = "relay-test-token"
CHAT_ID = "01993d50-ef7b-7b37-886b-23fd80c7ec10"
MESSAGE_ID = "01993d50-ef7b-7b37-886b-23fd80c7ec11"
EVENT_ID = "01993d50-ef7b-7b37-886b-23fd80c7ec12"


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
        "webhook_version": "2026-02-03",
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


def test_current_event_parsing_and_mentions():
    inbound = parse_inbound(event(group=True, mention=True))
    assert inbound is not None
    assert inbound.chat_id == CHAT_ID
    assert inbound.message_id == MESSAGE_ID
    assert inbound.is_group is True
    assert inbound.agent_handle == "helper"
    assert inbound.sender_name == "Advait"
    assert mentions_agent(inbound.message, handle=inbound.agent_handle)


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


def test_websocket_settings_and_ticket_routes():
    transport = FakeTransport([
        RelayResponse(200, {"enabled": False, "acked_through": "0"}),
        RelayResponse(200, {"enabled": True, "acked_through": "0"}),
        RelayResponse(200, {
            "url": "wss://events.example/ticket",
            "expires_at": "2026-08-29T00:01:00Z",
            "subprotocol": "relay.v1.json",
        }),
    ])
    client = RelayClient(TOKEN, transport=transport)
    asyncio.run(client.get_websocket_settings())
    asyncio.run(client.update_websocket(True))
    asyncio.run(client.create_websocket_connection())
    assert [call["path"] for call in transport.calls] == [
        "/v1/websocket",
        "/v1/websocket",
        "/v1/websocket-connections",
    ]
    assert transport.calls[1]["body"] == {"enabled": True}
    assert transport.calls[2]["body"] == {}


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
        self.order.append(f"ack:{frame['through_sequence']}")
        self.sent.append(frame)


def test_websocket_commits_before_cumulative_ack():
    order: List[str] = []
    socket = FakeSocket([
        {
            "type": "ready",
            "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
            "acked_through": "40",
            "heartbeat_interval_ms": 30_000,
            "max_in_flight": 64,
        },
        {"type": "event", "sequence": "41", "event": event()},
    ], order)
    inbox = OrderedInbox(order)
    with pytest.raises(RelayWebSocketClosed):
        asyncio.run(consume_websocket(socket, inbox=inbox))
    assert order == ["commit:41", "ack:41"]
    assert socket.sent == [{"type": "ack", "through_sequence": "41"}]


def test_websocket_refuses_to_ack_over_a_sequence_gap():
    order: List[str] = []
    socket = FakeSocket([
        {
            "type": "ready",
            "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
            "acked_through": "40",
            "heartbeat_interval_ms": 30_000,
            "max_in_flight": 64,
        },
        {"type": "event", "sequence": "42", "event": event()},
    ], order)
    with pytest.raises(RuntimeError, match="not contiguous"):
        asyncio.run(consume_websocket(socket, inbox=OrderedInbox(order)))
    assert order == []


@pytest.mark.parametrize("value", ["00", "01", "-1", "1.0"])
def test_websocket_rejects_noncanonical_decimal_sequences(value):
    order: List[str] = []
    socket = FakeSocket([
        {
            "type": "ready",
            "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
            "acked_through": "0",
            "heartbeat_interval_ms": 30_000,
            "max_in_flight": 64,
        },
        {"type": "event", "sequence": value, "event": event()},
    ], order)
    with pytest.raises(RuntimeError, match="invalid sequence"):
        asyncio.run(consume_websocket(socket, inbox=OrderedInbox(order)))
    assert order == []


def test_websocket_honors_error_and_disconnect_frames():
    ready = {
        "type": "ready",
        "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
        "acked_through": "0",
        "heartbeat_interval_ms": 30_000,
        "max_in_flight": 64,
    }
    retryable = FakeSocket([
        ready,
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
        "disabled",
        "replaced",
        "revoked",
        "heartbeat_timeout",
        "restart",
    ):
        disconnected = FakeSocket([
            ready,
            {"type": "disconnect", "reason": reason},
        ], [])
        with pytest.raises(RelayWebSocketDisconnect) as raised:
            asyncio.run(consume_websocket(disconnected, inbox=OrderedInbox([])))
        assert raised.value.reason == reason
        assert raised.value.terminal is (
            reason not in ("heartbeat_timeout", "restart")
        )

    unknown_error = FakeSocket([
        ready,
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

    invalid_ready = {
        "type": "ready",
        "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
        "acked_through": "0",
        "heartbeat_interval_ms": True,
        "max_in_flight": 64,
    }
    with pytest.raises(RuntimeError, match="invalid ready"):
        asyncio.run(consume_websocket(
            FakeSocket([invalid_ready], []),
            inbox=OrderedInbox([]),
        ))

    extra = event()
    frames = [
        {
            "type": "ready",
            "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
            "acked_through": "0",
            "heartbeat_interval_ms": 30_000,
            "max_in_flight": 64,
        },
        {"type": "event", "sequence": "1", "event": extra, "unexpected": True},
    ]
    with pytest.raises(RuntimeError, match="invalid frame"):
        asyncio.run(consume_websocket(
            FakeSocket(frames, []),
            inbox=OrderedInbox([]),
        ))


class FakeConnectionContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_exc):
        return None


def test_websocket_loop_reconnects_heartbeat_with_fresh_ticket_then_stops_disabled():
    ready = {
        "type": "ready",
        "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
        "acked_through": "0",
        "heartbeat_interval_ms": 30_000,
        "max_in_flight": 64,
    }
    sockets = [
        FakeSocket([
            ready,
            {"type": "disconnect", "reason": "heartbeat_timeout"},
        ], []),
        FakeSocket([
            ready,
            {"type": "disconnect", "reason": "restart"},
        ], []),
        FakeSocket([
            ready,
            {"type": "disconnect", "reason": "disabled"},
        ], []),
    ]

    class Client:
        def __init__(self):
            self.calls = 0

        async def create_websocket_connection(self):
            self.calls += 1
            return {
                "url": f"wss://relay.test/v1/websocket?ticket={self.calls}",
                "expires_at": "2026-08-29T00:01:00Z",
                "subprotocol": "relay.v1.json",
            }

    client = Client()
    connections = []
    sleeps = []

    def connect(url, *, subprotocols):
        connections.append((url, subprotocols))
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

    assert raised.value.reason == "disabled"
    assert client.calls == 3
    assert [entry[1] for entry in connections] == [
        ["relay.v1.json"],
        ["relay.v1.json"],
        ["relay.v1.json"],
    ]
    assert sleeps == [0.5, 0.5]


def test_websocket_loop_stops_on_protocol_violation_without_ticket_churn():
    socket = FakeSocket([
        {"type": "event", "sequence": "1", "event": event()},
    ], [])

    class Client:
        def __init__(self):
            self.calls = 0

        async def create_websocket_connection(self):
            self.calls += 1
            return {
                "url": "wss://relay.test/v1/websocket?ticket=one",
                "expires_at": "2026-08-29T00:01:00Z",
                "subprotocol": "relay.v1.json",
            }

    client = Client()
    with pytest.raises(RelayWebSocketProtocolError):
        asyncio.run(run_websocket_loop(
            client=client,
            inbox=OrderedInbox([]),
            connect_factory=lambda *_args, **_kwargs: FakeConnectionContext(socket),
            sleep=lambda _delay: None,
        ))
    assert client.calls == 1


def test_websocket_replay_is_deduplicated_but_acknowledged_again(tmp_path):
    frames = [
        {
            "type": "ready",
            "connection_id": "01993d50-ef7b-7b37-886b-23fd80c7ec17",
            "acked_through": "0",
            "heartbeat_interval_ms": 30_000,
            "max_in_flight": 64,
        },
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


def test_base_url_idempotency_and_errors():
    assert normalize_base_url(None) == "https://api.relayapp.im"
    assert normalize_base_url("http://localhost:8790") == "http://localhost:8790"
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
