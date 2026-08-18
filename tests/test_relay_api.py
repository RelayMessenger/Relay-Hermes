"""Transport tests. No network: every call goes through a fake transport."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_api import (  # noqa: E402
    MemoryDedupe,
    RelayApiError,
    RelayClient,
    RelayResponse,
    classify_status,
    is_consumer_takeover,
    normalize_base_url,
    parse_inbound,
    render_text,
    reply_idempotency_key,
    run_poll_loop,
    transient_delay_seconds,
)

TOKEN = "rly_live_test"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeTransport:
    """Records every request and replays a scripted list of responses."""

    def __init__(self, responses: List[Any]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> RelayResponse:
        self.calls.append(kwargs)
        if not self._responses:
            # An idle long poll: nothing new, cursor unchanged.
            return RelayResponse(status=200, body={"events": [], "next_cursor": 0})
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def events_page(events: List[Dict[str, Any]], next_cursor: int) -> RelayResponse:
    return RelayResponse(status=200, body={"events": events, "next_cursor": next_cursor})


def user_event(event_id: str, sender_id: str = "usr_owner", text: str = "hi",
               conversation_id: str = "cnv_1", invocation_id: Optional[str] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "message": {
            "id": f"msg_{event_id}",
            "conversation_id": conversation_id,
            "sender": {"kind": "user", "id": sender_id, "display_name": "Someone"},
            "parts": [{"type": "text", "text": text}],
            "created_at": "2026-08-18T00:00:00Z",
        }
    }
    if invocation_id:
        data["invocation_id"] = invocation_id
    return {
        "event_id": event_id,
        "event_type": "message.received",
        "agent_id": "agt_1",
        "created_at": "2026-08-18T00:00:00Z",
        "data": data,
    }


class Harness:
    """Cursor, dedupe and handler wiring shared by the poll-loop tests."""

    def __init__(self, allow_sender=None) -> None:
        self.cursor = 0
        self.dedupe = MemoryDedupe()
        self.handled: List[str] = []
        self.slept: List[float] = []
        self.logs: List[str] = []
        self.allow_sender = allow_sender
        self.fail_on: Optional[str] = None
        self.transport: Optional[FakeTransport] = None
        self.polls = 1
        self.polls_served = 0
        # True between a good page arriving and its cursor being saved, so the
        # poll budget never cuts a page off half way through.
        self.draining = False

    def set_cursor(self, value: int) -> None:
        self.cursor = value
        self.draining = False

    async def on_message(self, inbound) -> None:
        if self.fail_on and inbound.event_id == self.fail_on:
            raise RuntimeError("handler blew up")
        self.handled.append(inbound.event_id)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def should_continue(self) -> bool:
        """Bound the loop so a test cannot spin forever.

        In production this is the adapter's running flag, always True mid
        page. Here it also caps how many polls the loop may make, while
        letting a page that already arrived finish.
        """
        return self.draining or self.polls_served < self.polls

    async def run(self, transport: FakeTransport, polls: int = 1) -> None:
        self.transport = transport
        self.polls = polls

        async def counted(**kwargs: Any) -> RelayResponse:
            self.draining = False
            self.polls_served += 1
            response = await transport(**kwargs)
            self.draining = response.status < 400
            return response

        client = RelayClient(TOKEN, transport=counted)
        await run_poll_loop(
            client=client,
            get_cursor=lambda: self.cursor,
            set_cursor=self.set_cursor,
            dedupe=self.dedupe,
            on_message=self.on_message,
            allow_sender=self.allow_sender,
            should_continue=self.should_continue,
            log=self.logs.append,
            sleep=self.sleep,
            random_fn=lambda: 0.0,
        )


# ---------------------------------------------------------------------------
# Idempotency key derivation
# ---------------------------------------------------------------------------


def test_idempotency_key_is_derived_from_event_and_ordinal():
    assert reply_idempotency_key("evt_1", 0) == "reply-evt_1-0"
    assert reply_idempotency_key("evt_1", 2) == "reply-evt_1-2"


def test_idempotency_key_is_stable_across_retries():
    """The whole point: retrying the same reply must reuse the same key."""
    first = reply_idempotency_key("evt_9", 1)
    second = reply_idempotency_key("evt_9", 1)
    assert first == second


def test_idempotency_key_differs_per_reply_and_per_event():
    assert reply_idempotency_key("evt_1", 0) != reply_idempotency_key("evt_1", 1)
    assert reply_idempotency_key("evt_1", 0) != reply_idempotency_key("evt_2", 0)


def test_send_message_puts_the_key_on_the_wire():
    transport = FakeTransport([RelayResponse(status=200, body={"message_id": "msg_1"})])
    client = RelayClient(TOKEN, transport=transport)
    body = asyncio.run(
        client.send_message(
            "cnv_1", [{"type": "text", "text": "yo"}],
            idempotency_key=reply_idempotency_key("evt_7", 0),
            invocation_id="inv_3",
        )
    )
    assert body["message_id"] == "msg_1"
    call = transport.calls[0]
    assert call["headers"]["idempotency-key"] == "reply-evt_7-0"
    assert call["body"]["invocation_id"] == "inv_3"
    assert call["path"] == "/v1/messages"


# ---------------------------------------------------------------------------
# Cursor advance and failure
# ---------------------------------------------------------------------------


def test_cursor_advances_after_a_clean_page():
    harness = Harness()
    transport = FakeTransport([events_page([user_event("evt_1")], 12)])
    asyncio.run(harness.run(transport))
    assert harness.handled == ["evt_1"]
    assert harness.cursor == 12


def test_cursor_does_not_advance_when_the_handler_fails():
    """A failed handler must replay, so the cursor stays where it was."""
    harness = Harness()
    harness.fail_on = "evt_2"
    transport = FakeTransport([events_page([user_event("evt_1"), user_event("evt_2")], 30)])
    with pytest.raises(RuntimeError):
        asyncio.run(harness.run(transport))
    assert harness.handled == ["evt_1"]
    assert harness.cursor == 0
    # The failed event was never recorded, so redelivery is not mistaken for
    # a duplicate.
    assert not harness.dedupe.has("evt_2")
    assert harness.dedupe.has("evt_1")


def test_duplicate_event_is_handled_once():
    harness = Harness()
    transport = FakeTransport([
        events_page([user_event("evt_1")], 5),
        events_page([user_event("evt_1")], 6),
    ])
    asyncio.run(harness.run(transport, polls=2))
    assert harness.handled == ["evt_1"]
    assert harness.cursor == 6


def test_malformed_next_cursor_holds_position():
    harness = Harness()
    harness.cursor = 40
    transport = FakeTransport([RelayResponse(status=200, body={"events": [], "next_cursor": None})])
    asyncio.run(harness.run(transport))
    assert harness.cursor == 40


def test_agent_own_messages_are_skipped():
    harness = Harness()
    event = user_event("evt_1")
    event["data"]["message"]["sender"] = {"kind": "agent", "id": "agt_1"}
    asyncio.run(harness.run(FakeTransport([events_page([event], 3)])))
    assert harness.handled == []
    assert harness.cursor == 3


# ---------------------------------------------------------------------------
# Owner allowlist
# ---------------------------------------------------------------------------


def test_owner_allowlist_drops_other_senders():
    harness = Harness(allow_sender=lambda sender_id: sender_id == "usr_owner")
    transport = FakeTransport([
        events_page(
            [user_event("evt_ok", "usr_owner"), user_event("evt_no", "usr_stranger")], 9,
        )
    ])
    asyncio.run(harness.run(transport))
    assert harness.handled == ["evt_ok"]
    # A drop is a decided outcome, so it is recorded and not re-evaluated.
    assert harness.dedupe.has("evt_no")
    assert harness.cursor == 9


def test_allowlist_absent_lets_everyone_through():
    harness = Harness()
    transport = FakeTransport([events_page([user_event("evt_1", "usr_stranger")], 4)])
    asyncio.run(harness.run(transport))
    assert harness.handled == ["evt_1"]


# ---------------------------------------------------------------------------
# Backoff schedule
# ---------------------------------------------------------------------------


def test_backoff_schedule_matches_the_sdk_ladder():
    """500ms doubling, exponent capped at 6, clamped to 30s. Jitter off."""
    ladder = [transient_delay_seconds(n, lambda: 0.0) for n in range(1, 9)]
    assert ladder == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_backoff_adds_bounded_jitter():
    assert transient_delay_seconds(1, lambda: 0.999) == pytest.approx(1.249, abs=0.002)


def test_transient_error_backs_off_and_retries():
    harness = Harness()
    transport = FakeTransport([
        RelayResponse(status=503, body={"error": {"message": "upstream down"}}),
        events_page([user_event("evt_1")], 7),
    ])
    asyncio.run(harness.run(transport, polls=2))
    assert harness.slept == [1.0]
    assert harness.handled == ["evt_1"]
    assert harness.cursor == 7


def test_backoff_resets_after_a_good_poll():
    harness = Harness()
    transport = FakeTransport([
        RelayResponse(status=500, body={}),
        events_page([], 1),
        RelayResponse(status=500, body={}),
        events_page([], 2),
    ])
    asyncio.run(harness.run(transport, polls=4))
    assert harness.slept == [1.0, 1.0]


# ---------------------------------------------------------------------------
# Terminal failures
# ---------------------------------------------------------------------------


def test_terminated_by_other_consumer_stops_the_loop():
    harness = Harness()
    transport = FakeTransport([
        RelayResponse(
            status=409,
            body={"error": {
                "code": "terminated_by_other_consumer",
                "message": "a newer consumer took the token",
            }},
        ),
        events_page([user_event("evt_never")], 99),
    ])
    with pytest.raises(RelayApiError) as excinfo:
        asyncio.run(harness.run(transport, polls=2))
    error = excinfo.value
    assert is_consumer_takeover(error)
    assert error.status == 409
    # It stops rather than retrying: no sleep, no second poll, no events.
    assert harness.slept == []
    assert harness.handled == []
    assert harness.cursor == 0
    assert any("another consumer took this Agent Token" in line for line in harness.logs)


def test_unauthorized_stops_the_loop():
    harness = Harness()
    transport = FakeTransport([RelayResponse(status=401, body={"error": {"message": "bad token"}})])
    with pytest.raises(RelayApiError) as excinfo:
        asyncio.run(harness.run(transport, polls=2))
    assert excinfo.value.kind == "auth"
    assert harness.slept == []


def test_expired_cursor_reports_what_to_do():
    harness = Harness()
    transport = FakeTransport([RelayResponse(status=410, body={"error": {"message": "gone"}})])
    with pytest.raises(RelayApiError):
        asyncio.run(harness.run(transport, polls=2))
    assert any("Do not reset the cursor to zero" in line for line in harness.logs)


def test_cursor_ahead_of_ledger_names_the_resume_point():
    harness = Harness()
    transport = FakeTransport([
        RelayResponse(
            status=422,
            body={"error": {"message": "ahead", "details": {"highest_delivered_cursor": 88}}},
        )
    ])
    with pytest.raises(RelayApiError):
        asyncio.run(harness.run(transport, polls=2))
    assert any("resume from cursor 88" in line for line in harness.logs)


def test_status_classification():
    assert classify_status(401) == "auth"
    assert classify_status(409) == "conflict"
    assert classify_status(408) == "retryable"
    assert classify_status(429) == "retryable"
    assert classify_status(503) == "retryable"
    assert classify_status(400) == "rejected"
    assert classify_status(410) == "rejected"


# ---------------------------------------------------------------------------
# Base URL validation
# ---------------------------------------------------------------------------


def test_base_url_defaults_and_canonicalizes():
    assert normalize_base_url(None) == "https://api.relayapp.im"
    assert normalize_base_url("  https://api.relayapp.im/  ") == "https://api.relayapp.im"


def test_base_url_allows_loopback_http_only():
    assert normalize_base_url("http://localhost:8787") == "http://localhost:8787"
    assert normalize_base_url("http://127.0.0.1:8787") == "http://127.0.0.1:8787"
    with pytest.raises(ValueError):
        normalize_base_url("http://api.relayapp.im")


@pytest.mark.parametrize(
    "bad",
    [
        "https://user:pw@api.relayapp.im",
        "https://api.relayapp.im/v1",
        "https://api.relayapp.im?x=1",
        "not-a-url",
    ],
)
def test_base_url_rejects_surprising_origins(bad):
    with pytest.raises(ValueError):
        normalize_base_url(bad)


def test_client_requires_a_token():
    with pytest.raises(ValueError):
        RelayClient("   ")


# ---------------------------------------------------------------------------
# Poll request shape
# ---------------------------------------------------------------------------


def test_poll_clamps_the_long_poll_window():
    transport = FakeTransport([events_page([], 1), events_page([], 2)])
    client = RelayClient(TOKEN, transport=transport)
    asyncio.run(client.poll_events(4, timeout_seconds=900))
    asyncio.run(client.poll_events(4, timeout_seconds=0))
    assert transport.calls[0]["query"]["timeout"] == 30
    assert transport.calls[1]["query"]["timeout"] == 1
    assert transport.calls[0]["query"]["cursor"] == 4


def test_typing_threads_the_invocation_id():
    transport = FakeTransport([RelayResponse(status=204, body=None)])
    client = RelayClient(TOKEN, transport=transport)
    asyncio.run(client.set_typing("cnv_1", True, label="Thinking", invocation_id="inv_2"))
    call = transport.calls[0]
    assert call["path"] == "/v1/conversations/cnv_1/typing"
    assert call["body"] == {"started": True, "label": "Thinking", "invocation_id": "inv_2"}


# ---------------------------------------------------------------------------
# Event parsing and dedupe window
# ---------------------------------------------------------------------------


def test_parse_inbound_reads_group_invocation():
    inbound = parse_inbound(user_event("evt_1", invocation_id="inv_1"))
    assert inbound is not None
    assert inbound.is_group is True
    assert inbound.invocation_id == "inv_1"
    assert inbound.conversation_id == "cnv_1"
    assert inbound.sender_kind == "user"


def test_parse_inbound_ignores_other_event_types():
    assert parse_inbound({"event_type": "message.read", "data": {}}) is None
    assert parse_inbound({"event_type": "message.received", "data": {}}) is None


def test_render_text_joins_parts_in_order():
    message = {
        "parts": [
            {"type": "text", "text": "one"},
            {"type": "data", "data": {"k": 1}},
            {"type": "text", "text": "two"},
        ]
    }
    assert render_text(message) == 'one\n{"k": 1}\ntwo'


def test_render_text_falls_back_to_server_rendering():
    assert render_text({"parts": [{"type": "media"}], "fallback_text": "sent a photo"}) == "sent a photo"


def test_dedupe_window_is_bounded_and_ordered():
    dedupe = MemoryDedupe(capacity=2)
    dedupe.record("a")
    dedupe.record("b")
    dedupe.record("c")
    assert dedupe.has("a") is False
    assert dedupe.snapshot() == ["b", "c"]


def test_dedupe_restore_round_trips():
    dedupe = MemoryDedupe()
    dedupe.restore(["x", "y"])
    assert dedupe.has("x") and dedupe.has("y")


def test_a_shutting_down_loop_returns_quietly():
    """A failure that lands while the adapter is stopping is not an error.

    ``disconnect()`` clears the running flag mid-poll, so the in-flight
    request fails on a closed client. Raising there would turn every clean
    shutdown into a logged crash.
    """
    harness = Harness()
    transport = FakeTransport([RelayResponse(status=401, body={"error": {"message": "bad token"}})])
    asyncio.run(harness.run(transport, polls=1))
    assert harness.handled == []
    assert harness.logs == []
