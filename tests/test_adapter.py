from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = Path(
    os.environ.get("HERMES_AGENT_SRC", "").strip()
    or ROOT.parent / "_sources/hermes-agent"
)

pytestmark = pytest.mark.skipif(
    not HERMES_SOURCE.is_dir(),
    reason="Hermes source is required for adapter integration tests",
)


@pytest.fixture(scope="module")
def plugin():
    sys.path.insert(0, str(HERMES_SOURCE))

    from gateway.platform_registry import PlatformEntry, platform_registry

    if not platform_registry.is_registered("relayapp"):
        platform_registry.register(
            PlatformEntry(
                name="relayapp",
                label="Relay",
                adapter_factory=lambda config: None,
                check_fn=lambda: True,
            )
        )
    return importlib.import_module("relay_hermes.adapter")


def make_adapter(plugin, tmp_path):
    from gateway.config import PlatformConfig

    return plugin.RelayAdapter(PlatformConfig(extra={
        "token": "relay-test-token",
        "state_dir": str(tmp_path),
    }))


class FakeClient:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.uploads: List[Dict[str, Any]] = []
        self.reads: List[str] = []
        self._transfer_http = None

    async def mark_read(self, chat_id: str) -> None:
        self.reads.append(chat_id)

    async def send_message(
        self,
        chat_id: str,
        parts: List[Dict[str, Any]],
        *,
        idempotency_key: str,
        reply_to: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.calls.append({
            "chat_id": chat_id,
            "parts": parts,
            "idempotency_key": idempotency_key,
            "reply_to": reply_to,
            "timeout": timeout,
        })
        return {"chat_id": chat_id, "message": {"id": "message-id"}}

    async def upload_attachment(
        self,
        *,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> str:
        self.uploads.append({
            "filename": filename,
            "content_type": content_type,
            "data": data,
        })
        return "01993d50-ef7b-7b37-886b-23fd80c7ec16"

def relay_event(event_id: str = "event-id") -> Dict[str, Any]:
    return {
        "api_version": "v1",
        "webhook_version": "2026-08-30",
        "event_type": "message.received",
        "event_id": event_id,
        "created_at": "2026-08-29T00:00:00Z",
        "trace_id": "trace-id",
        "agent_id": "01993d50-ef7b-7b37-886b-23fd80c7ec13",
        "data": {
            "chat": {
                "id": "01993d50-ef7b-7b37-886b-23fd80c7ec10",
                "is_group": False,
            },
            "id": "01993d50-ef7b-7b37-886b-23fd80c7ec11",
            "direction": "inbound",
            "sender_handle": {
                "id": "01993d50-ef7b-7b37-886b-23fd80c7ec12",
                "handle": "advait",
                "joined_at": "2026-08-29T00:00:00Z",
                "kind": "user",
            },
            "parts": [{"type": "text", "value": "hello"}],
        },
    }


# A message older than the turn starter. Under the default "auto" quote
# rule a reply to the turn starter itself is unquoted, so tests that pin
# where the anchor lands answer this older message instead.
OLDER_MESSAGE_ID = "01993d50-ef7b-7b37-886b-23fd80c7ec01"


def message_event(plugin, adapter, event_id: str):
    from gateway.platforms.base import MessageEvent, MessageType

    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=adapter.build_source(
            chat_id="01993d50-ef7b-7b37-886b-23fd80c7ec10",
            chat_name="Relay Chat",
            chat_type="dm",
            user_id="01993d50-ef7b-7b37-886b-23fd80c7ec12",
            user_name="Advait",
            message_id="01993d50-ef7b-7b37-886b-23fd80c7ec11",
        ),
        message_id="01993d50-ef7b-7b37-886b-23fd80c7ec11",
        raw_message=relay_event(event_id),
    )


def test_text_send_uses_current_parts_and_retry_stable_distinct_keys(
    plugin,
    tmp_path,
):
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    event = message_event(plugin, adapter, "event-send")

    async def run():
        await adapter.on_processing_start(event)
        first = await adapter.send(
            event.source.chat_id,
            "one\n\ntwo",
            reply_to=OLDER_MESSAGE_ID,
        )
        second = await adapter.send(
            event.source.chat_id,
            "one\n\ntwo",
            reply_to=OLDER_MESSAGE_ID,
        )
        # A processing retry starts the same logical turn from ordinal zero.
        await adapter.on_processing_start(event)
        retried = await adapter.send(
            event.source.chat_id,
            "one\n\ntwo",
            reply_to=OLDER_MESSAGE_ID,
        )
        return first, second, retried

    first, second, retried = asyncio.run(run())
    assert first.success is True
    assert second.success is True
    assert retried.success is True
    assert first.message_id == "message-id"
    assert len(client.calls) == 3
    expected = {
        "chat_id": event.source.chat_id,
        "parts": [
            {"type": "text", "value": "one\n\ntwo"},
        ],
        "reply_to": {"message_id": OLDER_MESSAGE_ID},
        "timeout": None,
    }
    assert {key: value for key, value in client.calls[0].items()
            if key != "idempotency_key"} == expected
    assert client.calls[0]["idempotency_key"] != client.calls[1]["idempotency_key"]
    assert client.calls[0]["idempotency_key"] == client.calls[2]["idempotency_key"]
    assert client.calls[0]["idempotency_key"] == "reply-event-send-0"
    assert client.calls[1]["idempotency_key"] == "reply-event-send-1"


def test_concurrent_sends_share_turn_ordinals_without_crossing_turns(plugin, tmp_path):
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client

    async def turn(event_id):
        event = message_event(plugin, adapter, event_id)
        await adapter.on_processing_start(event)
        results = await asyncio.gather(
            adapter.send(event.source.chat_id, "first"),
            adapter.send(event.source.chat_id, "second"),
        )
        results.append(await adapter.send(event.source.chat_id, "third"))
        assert all(result.success for result in results)

    async def run():
        await asyncio.gather(turn("event-a"), turn("event-b"))
        await turn("event-a")

    asyncio.run(run())
    keys = [call["idempotency_key"] for call in client.calls]
    assert set(keys[:6]) == {
        f"reply-event-{event}-{ordinal}"
        for event in ("a", "b") for ordinal in range(3)
    }
    assert keys[6:] == [f"reply-event-a-{ordinal}" for ordinal in range(3)]


@pytest.mark.parametrize("outcome", ["ready", "auth", "timeout", "cancelled"])
def test_connect_waits_for_websocket_ready_and_cleans_failed_startup(
    plugin, tmp_path, monkeypatch, outcome,
):
    adapter = make_adapter(plugin, tmp_path)
    closed = []
    connected = []

    class Client(FakeClient):
        def __init__(self, *args):
            super().__init__()

        def open(self):
            pass

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(plugin, "RelayClient", Client)
    monkeypatch.setattr(plugin, "WEBSOCKET_READY_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(adapter, "_mark_connected", lambda: connected.append(True))

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def websocket(**kwargs):
            entered.set()
            await release.wait()
            if outcome == "auth":
                raise plugin.RelayApiError("rejected", kind="auth", status=401)
            kwargs["on_ready"]()
            await asyncio.Event().wait()

        monkeypatch.setattr(plugin, "run_websocket_loop", websocket)
        startup = asyncio.create_task(adapter.connect())
        await asyncio.wait_for(entered.wait(), 5)
        try:
            assert not startup.done()
            assert not connected
            if outcome == "cancelled":
                startup.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await startup
            else:
                if outcome != "timeout":
                    release.set()
                assert await startup is (outcome == "ready")
            if outcome == "ready":
                assert connected == [True]
                assert adapter._process_task is not None
            else:
                assert not connected
                assert adapter._receive_task is None
                assert adapter._process_task is None
                assert adapter._client is None
                assert closed == [True]
                if outcome == "auth":
                    assert adapter.fatal_error_code == "relay_unauthorized"
        finally:
            await adapter.disconnect()
            if not startup.done():
                startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)

    asyncio.run(run())


def test_processing_lifecycle_keeps_event_until_hermes_finishes(plugin, tmp_path):
    from gateway.platforms.base import ProcessingOutcome

    adapter = make_adapter(plugin, tmp_path)
    adapter._inbox.open()
    payload = relay_event("event-success")
    assert adapter._inbox.accept("1", payload) is True
    event_id, _ = adapter._inbox.next_pending()
    adapter._inbox.mark_dispatched(event_id)
    assert adapter._inbox.status(event_id) == "dispatched"

    event = message_event(plugin, adapter, event_id)
    asyncio.run(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))
    assert adapter._inbox.status(event_id) == "completed"

    failed = relay_event("event-failure")
    adapter._inbox.accept("2", failed)
    failed_id, _ = adapter._inbox.next_pending()
    adapter._inbox.mark_dispatched(failed_id)
    asyncio.run(adapter.on_processing_complete(
        message_event(plugin, adapter, failed_id),
        ProcessingOutcome.FAILURE,
    ))
    assert adapter._inbox.status(failed_id) == "pending"
    adapter._inbox.close()


@pytest.mark.parametrize("sender_kind", ["user", "agent"])
def test_inbox_dispatches_agent_sender_like_user_sender(
    plugin,
    tmp_path,
    monkeypatch,
    sender_kind,
):
    adapter = make_adapter(plugin, tmp_path)
    adapter._inbox.open()
    payload = relay_event(f"event-{sender_kind}")
    payload["data"]["sender_handle"]["kind"] = sender_kind
    assert adapter._inbox.accept("1", payload) is True

    received = []

    async def capture(inbound):
        received.append(inbound)
        adapter._running = False
        return True

    original_complete = adapter._inbox.complete

    def stop_if_ignored(event_id, *, ignored=False):
        original_complete(event_id, ignored=ignored)
        adapter._running = False

    monkeypatch.setattr(adapter, "_on_inbound", capture)
    monkeypatch.setattr(adapter._inbox, "complete", stop_if_ignored)
    adapter._message_handler = object()
    adapter._running = True

    asyncio.run(adapter._process_inbox())

    assert [inbound.sender_contact_kind for inbound in received] == [sender_kind]
    assert adapter._inbox.status(payload["event_id"]) == "dispatched"
    adapter._inbox.close()


def test_read_is_sent_at_intake_before_any_processing_hook(plugin, tmp_path):
    """A message Hermes folds into a running turn never reaches
    on_processing_start, so Read must already be on the wire at intake."""
    api = importlib.import_module("relay_hermes.relay_api")
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    dispatched = []

    async def capture_dispatch(event):
        dispatched.append(event)

    adapter._dispatch_turn = capture_dispatch
    inbound = api.parse_inbound(relay_event("event-read"))
    assert inbound is not None

    async def intake_only():
        assert await adapter._on_inbound(inbound) is True
        # Let the fire-and-forget receipt task run; no processing hook yet.
        await asyncio.sleep(0)
        return list(client.reads)

    assert asyncio.run(intake_only()) == [inbound.chat_id]
    assert len(dispatched) == 1

    asyncio.run(adapter.on_processing_start(dispatched[0]))
    assert client.reads == [inbound.chat_id], "processing start must not send a second Read"


def test_auto_quote_ignores_a_folded_message_but_quotes_an_older_turn_starter(
    plugin, tmp_path
):
    """Under Hermes's default busy_input_mode: interrupt a second message
    that lands mid-turn is folded into the running turn: it never reaches
    on_processing_start, and the reply stays anchored to the turn starter."""
    api = importlib.import_module("relay_hermes.relay_api")
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    chat_id = "01993d50-ef7b-7b37-886b-23fd80c7ec10"

    async def capture_dispatch(event):
        pass

    adapter._dispatch_turn = capture_dispatch

    def inbound_with(message_id, event_id):
        payload = relay_event(event_id)
        payload["data"]["id"] = message_id
        return api.parse_inbound(payload)

    async def folded_then_reply():
        # "Nice" starts a turn.
        starter = message_event(plugin, adapter, "event-nice")
        await adapter.on_processing_start(starter)
        # "Whatsup gang" arrives mid-turn: intake runs, no processing hook.
        folded = inbound_with("01993d50-ef7b-7b37-886b-23fd80c7ec99", "event-whatsup")
        assert await adapter._on_inbound(folded) is True
        # Hermes anchors the reply to the turn starter.
        await adapter.send(chat_id, "hey!", reply_to=starter.message_id)

    asyncio.run(folded_then_reply())
    assert client.calls[-1]["reply_to"] is None

    async def older_turn_starter():
        first = message_event(plugin, adapter, "event-first")
        await adapter.on_processing_start(first)
        second = inbound_with("01993d50-ef7b-7b37-886b-23fd80c7ec98", "event-second")
        assert await adapter._on_inbound(second) is True
        await adapter.on_processing_start(
            _event_with_message_id(plugin, adapter, "event-second", second.message_id)
        )
        # A reply that answers the FIRST message, which is no longer the last
        # turn starter, still quotes it.
        await adapter.send(chat_id, "about the first one", reply_to=first.message_id)

    asyncio.run(older_turn_starter())
    assert client.calls[-1]["reply_to"] == {"message_id": "01993d50-ef7b-7b37-886b-23fd80c7ec11"}


class RecordingInbox:
    """Stands in for the SQLite inbox: records lifecycle calls only."""

    def __init__(self) -> None:
        self.completed: List[str] = []
        self.retried: List[str] = []

    def complete(self, event_id: str, *, ignored: bool = False) -> None:
        self.completed.append(event_id)

    def retry(self, event_id: str, error: str) -> None:
        self.retried.append(event_id)


@pytest.mark.parametrize(
    "hermes_outcome,expect_completed",
    [
        ("folded", True),      # busy session, redirect swallowed the text
        ("queued", False),     # busy session, event became the pending turn
        ("overflow", False),   # busy session, accepted into the invisible FIFO
        ("started", False),    # idle session, a turn started for this event
    ],
)
def test_dispatch_settles_only_a_folded_event(
    plugin, tmp_path, hermes_outcome, expect_completed
):
    """Hermes calls no processing hook for a message it folds into a running
    turn, so the durable row would stay dispatched and replay on restart. An
    event Hermes accepted anywhere is left for its own hooks to settle."""
    adapter = make_adapter(plugin, tmp_path)
    inbox = RecordingInbox()
    adapter._inbox = inbox
    event = message_event(plugin, adapter, "event-fold")
    session_key = adapter._event_session_key(event)

    async def fake_handle_message(incoming):
        incoming._gateway_accepted = False
        if hermes_outcome == "folded":
            adapter._active_sessions[session_key] = asyncio.Event()
        elif hermes_outcome == "queued":
            adapter._active_sessions[session_key] = asyncio.Event()
            adapter._pending_messages[session_key] = incoming
            incoming._gateway_accepted = True
        elif hermes_outcome == "overflow":
            adapter._active_sessions[session_key] = asyncio.Event()
            incoming._gateway_accepted = True
        elif hermes_outcome == "started":
            adapter._active_sessions[session_key] = asyncio.Event()
            incoming._gateway_accepted = True

    adapter.handle_message = fake_handle_message
    asyncio.run(adapter._dispatch_turn(event))

    assert inbox.completed == (["event-fold"] if expect_completed else [])
    assert inbox.retried == []


def test_tool_progress_lines_are_eaten_for_relay(plugin, tmp_path):
    """Relay bubbles cannot be edited, so tool chrome would land as its own
    permanent message. Hermes drops an event the adapter renders as None."""
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.stream_events import ToolCallChunk

    adapter = make_adapter(plugin, tmp_path)
    chunk = ToolCallChunk(tool_name="terminal", preview="ls")
    # The base rendering is real chrome; this adapter refuses it.
    assert BasePlatformAdapter.format_tool_event(adapter, chunk)
    assert adapter.format_tool_event(chunk) is None
    assert adapter.format_tool_event(chunk, mode="verbose", preview_max_len=0) is None


def _event_with_message_id(plugin, adapter, event_id, message_id):
    from gateway.platforms.base import MessageEvent, MessageType

    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=adapter.build_source(
            chat_id="01993d50-ef7b-7b37-886b-23fd80c7ec10",
            chat_name="Relay Chat",
            chat_type="dm",
            user_id="01993d50-ef7b-7b37-886b-23fd80c7ec12",
            user_name="Advait",
            message_id=message_id,
        ),
        message_id=message_id,
        raw_message=relay_event(event_id),
    )


def test_failed_read_receipt_never_blocks_intake(plugin, tmp_path):
    api = importlib.import_module("relay_hermes.relay_api")
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()

    async def failing_mark_read(chat_id):
        raise api.RelayApiError("read failed")

    client.mark_read = failing_mark_read
    adapter._client = client
    dispatched = []

    async def capture_dispatch(event):
        dispatched.append(event)

    adapter._dispatch_turn = capture_dispatch
    inbound = api.parse_inbound(relay_event("event-read-fail"))

    async def run():
        assert await adapter._on_inbound(inbound) is True
        tasks = list(adapter._read_tasks)
        assert len(tasks) == 1
        await asyncio.sleep(0)
        # The receipt failure is logged and swallowed inside the task, never
        # left as an unretrieved exception and never raised into intake.
        assert tasks[0].done() and tasks[0].exception() is None

    asyncio.run(run())
    assert len(dispatched) == 1


def test_full_sync_rebuilds_and_durably_checkpoints_adapter_state(
    plugin,
    tmp_path,
):
    adapter = make_adapter(plugin, tmp_path)
    adapter._inbox.open()
    direct_chat = "01993d50-ef7b-7b37-886b-23fd80c7ec10"
    group_chat = "01993d50-ef7b-7b37-886b-23fd80c7ec20"
    old_inbound = "01993d50-ef7b-7b37-886b-23fd80c7ec11"
    new_inbound = "01993d50-ef7b-7b37-886b-23fd80c7ec12"
    snapshot = [
        {
            "chat": {"id": direct_chat, "is_group": False},
            "messages": [
                {
                    "id": new_inbound,
                    "chat_id": direct_chat,
                    "is_from_me": False,
                    "is_system_message": False,
                    "created_at": "2026-08-29T02:00:00Z",
                },
                {
                    "id": old_inbound,
                    "chat_id": direct_chat,
                    "is_from_me": False,
                    "is_system_message": False,
                    "created_at": "2026-08-29T01:00:00Z",
                },
            ],
        },
        {
            "chat": {"id": group_chat, "is_group": True},
            "messages": [],
        },
    ]

    class FullSyncClient:
        async def fetch_full_snapshot(self):
            return snapshot

    adapter._client = FullSyncClient()
    asyncio.run(adapter._on_full_sync(
        "42",
        "checkpoint_outside_retention",
    ))

    assert adapter._direct_chats == {direct_chat}
    assert adapter._group_chats == {group_chat}
    assert adapter._last_inbound == {direct_chat: new_inbound}
    assert adapter._inbox.full_sync_state()["through_sequence"] == "42"
    assert adapter._inbox.load_full_snapshot()[0]["chat"]["id"] == direct_chat
    adapter._inbox.close()


def test_full_sync_fails_closed_before_checkpoint_on_unreconcilable_chat(
    plugin,
    tmp_path,
):
    adapter = make_adapter(plugin, tmp_path)
    adapter._inbox.open()

    class InvalidFullSyncClient:
        async def fetch_full_snapshot(self):
            return [{
                "chat": {
                    "id": "01993d50-ef7b-7b37-886b-23fd80c7ec10",
                },
                "messages": [],
            }]

    adapter._client = InvalidFullSyncClient()
    with pytest.raises(plugin.RelayFullSyncError, match="cannot safely"):
        asyncio.run(adapter._on_full_sync(
            "42",
            "checkpoint_outside_retention",
        ))
    assert adapter._inbox.full_sync_state() is None
    adapter._inbox.close()


def test_bubble_chunking_uses_utf16_and_preserves_code_fences(plugin):
    assert plugin._bubble_chunks("one\n\n```\na\n\nb\n```\n\ntwo") == [
        "one",
        "```\na\n\nb\n```",
        "two",
    ]
    chunks = plugin._bubble_chunks("😀" * 5_001)
    assert len(chunks) == 2
    assert all(plugin.utf16_len(chunk) <= plugin.MAX_MESSAGE_LENGTH for chunk in chunks)
    assert "".join(chunks) == "😀" * 5_001


def test_part_folding_stays_under_the_contract_limit_without_merging_media(plugin):
    parts = [{"type": "text", "value": str(index)} for index in range(101)]
    folded = plugin._fold_parts(parts)
    assert folded == [{
        "type": "text",
        "value": "\n\n".join(str(index) for index in range(101)),
    }]

    media = [{"type": "media", "url": f"https://files.example/{index}"}
             for index in range(101)]
    assert plugin._fold_parts(media) == media


def test_hosted_image_batch_is_one_ordered_message(plugin, tmp_path):
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    asyncio.run(adapter.send_multiple_images(
        "chat-id",
        [
            ("https://files.example/one.png", "first"),
            ("https://files.example/two.png", "second"),
        ],
    ))
    assert len(client.calls) == 1
    assert client.calls[0]["parts"] == [
        {"type": "media", "url": "https://files.example/one.png"},
        {"type": "media", "url": "https://files.example/two.png"},
        {"type": "text", "value": "first\n\nsecond"},
    ]


def test_long_text_batches_never_post_adjacent_text_parts_and_retry_keys_are_stable(
    plugin, tmp_path,
):
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    event = message_event(plugin, adapter, "event-long-text")
    paragraphs = ["😀" * 5_000, "second paragraph", "third paragraph"]

    async def run():
        await adapter.on_processing_start(event)
        first = await adapter.send(event.source.chat_id, "\n\n".join(paragraphs),
                                   reply_to=OLDER_MESSAGE_ID)
        original = list(client.calls)
        client.calls.clear()
        await adapter.on_processing_start(event)
        replayed = await adapter.send(event.source.chat_id, "\n\n".join(paragraphs),
                                      reply_to=OLDER_MESSAGE_ID)
        return first, replayed, original

    first, replayed, original = asyncio.run(run())
    assert first.success and replayed.success
    assert len(original) == 2
    assert original == client.calls
    assert len({call["idempotency_key"] for call in original}) == 2
    assert original[0]["reply_to"] == {"message_id": OLDER_MESSAGE_ID}
    assert original[1]["reply_to"] is None
    assert "\n\n".join(part["value"] for call in original for part in call["parts"]) == "\n\n".join(paragraphs)
    for call in original:
        assert len(call["parts"]) <= plugin.MAX_PARTS_PER_POST
        assert all(plugin.utf16_len(part["value"]) <= plugin.MAX_MESSAGE_LENGTH
                   for part in call["parts"])
        assert not any(a["type"] == b["type"] == "text"
                       for a, b in zip(call["parts"], call["parts"][1:]))


def test_audio_file_uses_attachment_upload_then_idempotent_message_route(
    plugin,
    tmp_path,
    monkeypatch,
):
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    audio = tmp_path / "memo.mp3"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(adapter, "validate_media_delivery_path", lambda path: path)

    event = message_event(plugin, adapter, "event-audio")

    async def run():
        await adapter.on_processing_start(event)
        return await adapter.send_voice("chat-id", str(audio))

    result = asyncio.run(run())
    assert result.success is True
    assert result.message_id == "message-id"
    assert client.uploads == [{
        "filename": "memo.mp3",
        "content_type": "audio/mpeg",
        "data": b"audio",
    }]
    assert client.calls == [{
        "chat_id": "chat-id",
        "parts": [{
            "type": "media",
            "attachment_id": "01993d50-ef7b-7b37-886b-23fd80c7ec16",
        }],
        "idempotency_key": "reply-event-audio-0",
        "reply_to": None,
        "timeout": None,
    }]


def test_unmentioned_group_event_is_not_dispatched(plugin, tmp_path):
    api = importlib.import_module("relay_hermes.relay_api")
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    payload = relay_event()
    payload["data"]["chat"] = {
        "id": payload["data"]["chat"]["id"],
        "is_group": True,
        "owner_handle": {
            "id": "01993d50-ef7b-7b37-886b-23fd80c7ec13",
            "handle": "helper.developer",
            "joined_at": "2026-08-29T00:00:00Z",
            "kind": "agent",
            "is_me": True,
        },
    }
    inbound = api.parse_inbound(payload)
    assert inbound is not None
    assert asyncio.run(adapter._on_inbound(inbound)) is False
    assert client.reads == []


def test_register_loads_as_a_hermes_platform_plugin(plugin):
    calls = []

    class Context:
        def register_platform(self, **kwargs):
            calls.append(kwargs)

    plugin.register(Context())
    assert len(calls) == 1
    assert calls[0]["name"] == "relayapp"
    assert calls[0]["required_env"] == ["RELAY_AGENT_TOKEN"]
    assert calls[0]["allowed_users_env"] == "RELAY_ALLOWED_CONTACTS"
    assert "allow_all_env" not in calls[0]
    assert calls[0]["allow_update_command"] is False


def test_env_enablement_keeps_chat_allowlist_without_operator_support(
    plugin,
    monkeypatch,
):
    monkeypatch.setenv("RELAY_AGENT_TOKEN", "relay-test-token")
    monkeypatch.setenv("RELAY_ALLOWED_CONTACTS", "contact-one,contact-two")
    monkeypatch.setenv("RELAY_OPERATOR_CONTACTS", "must-be-ignored")
    monkeypatch.delenv("RELAY_ALLOW_ALL_CONTACTS", raising=False)
    first = plugin._env_enablement()
    second = plugin._env_enablement()
    assert first["allowed_contacts"] == ["contact-one", "contact-two"]
    assert first["allowed_users"] == ["contact-one", "contact-two"]
    assert "operator_contacts" not in first
    assert "allow_admin_from" not in first
    assert "group_allow_admin_from" not in first
    assert second == first
    assert "RELAY_ALLOW_ALL_CONTACTS" not in os.environ


def test_ordinary_contacts_continue_to_dispatch_normal_chat(
    plugin,
    tmp_path,
):
    api = importlib.import_module("relay_hermes.relay_api")
    from gateway.config import PlatformConfig

    config = PlatformConfig(extra={
        "token": "relay-test-token",
        "state_dir": str(tmp_path),
        "allowed_contacts": ["ordinary-contact"],
    })
    adapter = plugin.RelayAdapter(config)

    assert adapter._allow_contact("ordinary-contact") is True
    assert config.extra["allowed_users"] == ["ordinary-contact"]
    dispatched = []

    async def capture_dispatch(event):
        dispatched.append(event)

    adapter._dispatch_turn = capture_dispatch
    inbound = api.parse_inbound(relay_event("ordinary-chat"))
    assert inbound is not None
    assert asyncio.run(adapter._on_inbound(inbound)) is True
    assert [event.text for event in dispatched] == ["hello"]


def test_relay_slash_policy_is_always_deny_only(
    plugin,
    tmp_path,
):
    from gateway.config import PlatformConfig
    from gateway.slash_access import policy_from_extra

    config = PlatformConfig(extra={
        "token": "relay-test-token",
        "state_dir": str(tmp_path),
        # Every attempted legacy/native grant is retired by the adapter.
        "operator_contacts": ["ordinary-contact"],
        "allow_admin_from": ["ordinary-contact"],
        "group_allow_admin_from": ["ordinary-contact"],
        "user_allowed_commands": ["update"],
        "group_user_allowed_commands": ["update"],
    })
    adapter = plugin.RelayAdapter(config)
    assert adapter._allow_contact("ordinary-contact") is True
    assert "operator_contacts" not in config.extra
    assert config.extra["allow_admin_from"] == [
        plugin.NO_RELAY_SLASH_ADMIN
    ]
    assert config.extra["group_allow_admin_from"] == [
        plugin.NO_RELAY_SLASH_ADMIN
    ]
    assert config.extra["user_allowed_commands"] == []
    assert config.extra["group_user_allowed_commands"] == []

    for scope in ("dm", "group"):
        policy = policy_from_extra(config.extra, scope)
        assert policy.enabled is True
        assert policy.can_run("ordinary-contact", "update") is False
        assert policy.can_run("ordinary-contact", "approvals") is False
    assert plugin._is_slash_message("/update") is True
    assert plugin._is_slash_message("  /help") is True
    assert plugin._is_slash_message("ordinary chat") is False


def test_pinned_hermes_multi_profile_slash_dispatch_denies_update_everywhere(
    plugin,
    tmp_path,
):
    """Pin the source.profile bug and prove Relay fails closed around it."""

    api = importlib.import_module("relay_hermes.relay_api")
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.slash_access import policy_for_source

    primary_config = PlatformConfig(extra={
        "token": "primary-token",
        "state_dir": str(tmp_path / "primary-state"),
        "allowed_contacts": ["primary-contact"],
        # These attempted grants must not survive adapter construction.
        "allow_admin_from": ["primary-contact", "secondary-contact"],
        "user_allowed_commands": ["update"],
    })
    secondary_config = PlatformConfig(extra={
        "token": "secondary-token",
        "state_dir": str(tmp_path / "secondary-state"),
        "allowed_contacts": ["secondary-contact"],
        "allow_admin_from": ["secondary-contact"],
        "user_allowed_commands": ["update"],
    })
    primary = plugin.RelayAdapter(primary_config)
    secondary = plugin.RelayAdapter(secondary_config)
    secondary.set_owner_profile("secondary")

    # This is the exact pinned-Hermes seam: policy_for_source ignores
    # source.profile and consults only gateway_config.platforms (the primary
    # config). The primary deny-only policy therefore covers both sources.
    gateway_config = GatewayConfig(
        platforms={primary.platform: primary_config}
    )
    primary_source = primary.build_source(
        chat_id="primary-chat",
        chat_name="Primary",
        chat_type="dm",
        user_id="primary-contact",
        user_name="Primary Contact",
    )
    primary_source.profile = "default"
    secondary_source = secondary.build_source(
        chat_id="secondary-chat",
        chat_name="Secondary",
        chat_type="dm",
        user_id="secondary-contact",
        user_name="Secondary Contact",
    )
    secondary_source.profile = "secondary"
    assert policy_for_source(
        gateway_config, primary_source
    ).can_run("primary-contact", "update") is False
    assert policy_for_source(
        gateway_config, secondary_source
    ).can_run("secondary-contact", "update") is False

    dispatched = []

    async def capture_dispatch(event):
        dispatched.append(event)

    primary._dispatch_turn = capture_dispatch
    secondary._dispatch_turn = capture_dispatch

    def inbound(event_id: str, contact_id: str, text: str):
        payload = relay_event(event_id)
        payload["data"]["sender_handle"]["id"] = contact_id
        payload["data"]["parts"] = [{"type": "text", "value": text}]
        parsed = api.parse_inbound(payload)
        assert parsed is not None
        return parsed

    assert asyncio.run(primary._on_inbound(
        inbound("primary-update", "primary-contact", "/update")
    )) is False
    assert asyncio.run(secondary._on_inbound(
        inbound("secondary-update", "secondary-contact", "/update")
    )) is False
    assert dispatched == []

    # The profile-independent slash block must not disable ordinary chat.
    assert asyncio.run(primary._on_inbound(
        inbound("primary-chat", "primary-contact", "hello primary")
    )) is True
    assert asyncio.run(secondary._on_inbound(
        inbound("secondary-chat", "secondary-contact", "hello secondary")
    )) is True
    assert [event.text for event in dispatched] == [
        "hello primary",
        "hello secondary",
    ]


def test_invalid_configured_base_url_fails_closed_without_production_fallback(
    plugin,
    monkeypatch,
    tmp_path,
):
    from gateway.config import PlatformConfig

    monkeypatch.setenv("RELAY_AGENT_TOKEN", "relay-test-token")
    monkeypatch.setenv("RELAY_BASE_URL", "/")
    enabled = plugin._env_enablement()
    assert enabled["base_url"] == "/"
    config = PlatformConfig(extra={
        **enabled,
        "state_dir": str(tmp_path),
    })

    assert plugin.check_requirements() is False
    assert plugin.validate_config(config) is False
    with pytest.raises(ValueError, match="invalid base URL|must be an origin"):
        plugin.RelayAdapter(config)
    standalone = asyncio.run(
        plugin._standalone_send(config, "chat-id", "must not send")
    )
    assert "invalid base URL" in standalone["error"]
    assert "api.relayapp.im/v1" not in standalone["error"]


def test_adapter_uses_normalized_origin_for_its_state_binding(plugin, tmp_path):
    from gateway.config import PlatformConfig

    adapter = plugin.RelayAdapter(PlatformConfig(extra={
        "token": "relay-test-token",
        "base_url": "HTTPS://API.STAGING.RELAYAPP.IM:443/",
        "state_dir": str(tmp_path),
    }))
    assert adapter._base_url == "https://api.staging.relayapp.im"
    assert adapter._inbox._binding.api_origin == adapter._base_url


def test_yaml_contact_allowlist_accepts_current_list_vocabulary(plugin, tmp_path):
    from gateway.config import PlatformConfig

    adapter = plugin.RelayAdapter(PlatformConfig(extra={
        "token": "relay-test-token",
        "state_dir": str(tmp_path),
        "allowed_contacts": ["contact-one", "contact-two"],
    }))
    assert adapter._allowed_contacts == {"contact-one", "contact-two"}


def test_multiplexed_profiles_never_fall_through_to_process_relay_settings(
    plugin,
    tmp_path,
    monkeypatch,
):
    from agent.secret_scope import (
        is_multiplex_active,
        reset_secret_scope,
        set_multiplex_active,
        set_secret_scope,
    )
    from gateway.config import PlatformConfig
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    process_state = tmp_path / "process-state"
    monkeypatch.setenv("RELAY_AGENT_TOKEN", "process-token-must-not-leak")
    monkeypatch.setenv("RELAY_BASE_URL", "https://process.invalid")
    monkeypatch.setenv("RELAY_ALLOWED_CONTACTS", "process-contact")
    monkeypatch.setenv("RELAY_OPERATOR_CONTACTS", "retired-process-setting")
    monkeypatch.setenv("RELAY_STATE_DIR", str(process_state))

    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)

    def build(home: Path, secrets: dict[str, str]):
        home.mkdir()
        home_token = set_hermes_home_override(str(home))
        secret_token = set_secret_scope(secrets)
        try:
            seed = plugin._env_enablement()
            if seed is None:
                assert plugin.check_requirements() is False
                assert plugin.validate_config(PlatformConfig()) is False
                return None, None
            config = PlatformConfig(extra=seed)
            return plugin.RelayAdapter(config), config
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)

    try:
        profile_a_state = tmp_path / "profile-a-state"
        profile_a, config_a = build(
            tmp_path / "profile-a",
            {
                "RELAY_AGENT_TOKEN": "profile-a-token",
                "RELAY_BASE_URL": "https://a.example.test",
                "RELAY_ALLOWED_CONTACTS": "a-contact",
                "RELAY_OPERATOR_CONTACTS": "retired-profile-setting",
                "RELAY_STATE_DIR": str(profile_a_state),
            },
        )
        profile_b, config_b = build(
            tmp_path / "profile-b",
            {
                "RELAY_AGENT_TOKEN": "profile-b-token",
            },
        )
        missing, missing_config = build(
            tmp_path / "profile-missing",
            {},
        )
    finally:
        set_multiplex_active(previous_multiplex)

    assert profile_a is not None and config_a is not None
    assert profile_b is not None and config_b is not None
    assert missing is None and missing_config is None

    assert profile_a._token == "profile-a-token"
    assert profile_a._base_url == "https://a.example.test"
    assert profile_a._allowed_contacts == {"a-contact"}
    assert profile_a._inbox.path.parent == profile_a_state
    assert config_a.extra["allow_admin_from"] == [
        plugin.NO_RELAY_SLASH_ADMIN
    ]
    assert "operator_contacts" not in config_a.extra

    assert profile_b._token == "profile-b-token"
    assert profile_b._base_url == plugin.DEFAULT_BASE_URL
    assert profile_b._allowed_contacts == set()
    assert profile_b._inbox.path.parent == tmp_path / "profile-b" / "relay"
    assert config_b.extra["allowed_users"] == ["*"]
    assert config_b.extra["allow_admin_from"] == [
        plugin.NO_RELAY_SLASH_ADMIN
    ]

    assert profile_a._inbox.path != profile_b._inbox.path
    assert profile_a._inbox.path.parent != process_state
    assert profile_b._inbox.path.parent != process_state
    assert os.environ["RELAY_AGENT_TOKEN"] == "process-token-must-not-leak"

    profile_a._inbox.open()
    profile_b._inbox.open()
    try:
        assert profile_a._inbox.accept(
            "1",
            {"event_id": "profile-a-event", "data": {}},
        ) is True
        assert profile_b._inbox.accept(
            "1",
            {"event_id": "profile-b-event", "data": {}},
        ) is True
        assert profile_a._inbox.status("profile-a-event") == "pending"
        assert profile_a._inbox.status("profile-b-event") is None
        assert profile_b._inbox.status("profile-b-event") == "pending"
        assert profile_b._inbox.status("profile-a-event") is None
    finally:
        profile_a._inbox.close()
        profile_b._inbox.close()


def assert_batch_contract(plugin, calls):
    for call in calls:
        parts = call["parts"]
        assert 1 <= len(parts) <= 100
        assert sum(p["type"] == "media" and "url" in p for p in parts) <= 40
        assert not any(a["type"] == b["type"] == "text"
                       for a, b in zip(parts, parts[1:]))
        assert all(plugin.utf16_len(p["value"]) <= 10_000
                   for p in parts if p["type"] == "text")


def test_standalone_long_paragraphs_and_fixed_clock_keys(plugin, monkeypatch):
    import httpx
    from gateway.config import PlatformConfig

    requests = []
    real_client = plugin.RelayClient

    def respond(request):
        import json
        body = json.loads(request.content)
        key = request.headers["Idempotency-Key"]
        requests.append({"parts": body["message"]["parts"], "key": key})
        return httpx.Response(200, json={"message": {"id": "first-id"}})

    def client(token, base_url):
        instance = real_client(token, base_url)
        instance._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        return instance

    monkeypatch.setattr(plugin, "RelayClient", client)
    monkeypatch.setattr(plugin.time, "time_ns", lambda: 123456)
    paragraphs = ["😀" * 5000, "b" * 9999, "last", "paragraph"]
    result = asyncio.run(plugin._standalone_send(
        PlatformConfig(extra={"token": "fixture"}), "chat-id", "\n\n".join(paragraphs)))
    assert result.get("success"), result
    assert_batch_contract(plugin, requests)
    assert len(requests) == 3
    original = list(requests)
    requests.clear()
    replay = asyncio.run(plugin._standalone_send(
        PlatformConfig(extra={"token": "fixture"}), "chat-id", "\n\n".join(paragraphs)))
    assert replay == result
    assert requests == original
    assert [r["key"] for r in requests] == [
        f"hermes-cron-chat-id-123456-{i}" for i in range(3)]
    assert "\n\n".join(p["value"] for r in requests for p in r["parts"]) == "\n\n".join(paragraphs)


@pytest.mark.parametrize("uploaded,count,sizes", [(False, 41, [40, 1]),
                                                   (True, 100, [100]),
                                                   (True, 101, [100, 1])])
def test_image_batch_caps_and_replay(plugin, tmp_path, monkeypatch, uploaded, count, sizes):
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    event = message_event(plugin, adapter, "event-image-batches")

    async def upload(path):
        return "01993d50-ef7b-7b37-886b-23fd80c7ec16", None

    monkeypatch.setattr(adapter, "_upload_attachment", upload)
    images = [(f"file:///fixture/{i}" if uploaded else f"https://files.example/{i}", "")
              for i in range(count)]

    async def run():
        await adapter.on_processing_start(event)
        await adapter.send_multiple_images(event.source.chat_id, images)
        original = list(client.calls)
        client.calls.clear()
        await adapter.on_processing_start(event)
        await adapter.send_multiple_images(event.source.chat_id, images)
        return original

    original = asyncio.run(run())
    assert_batch_contract(plugin, original)
    assert [len(c["parts"]) for c in original] == sizes
    assert original == client.calls
    assert len({c["idempotency_key"] for c in original}) == len(sizes)
    assert sum(len(c["parts"]) for c in original) == count


def test_shared_batching_mixed_boundaries_and_first_anchor(plugin, tmp_path):
    adapter = make_adapter(plugin, tmp_path)
    client = FakeClient()
    adapter._client = client
    event = message_event(plugin, adapter, "event-mixed-batches")
    media = [{"type": "media", "url": f"https://files.example/{i}"} for i in range(41)]
    attachment = {"type": "media", "attachment_id": "01993d50-ef7b-7b37-886b-23fd80c7ec16"}
    parts = [{"type": "text", "value": "first"}, {"type": "text", "value": "paragraph"},
             *media[:40], attachment, {"type": "text", "value": "😀" * 5000},
             {"type": "text", "value": "next"}, media[40],
             {"type": "text", "value": "last"}]

    async def run():
        await adapter.on_processing_start(event)
        return await adapter._commit(event.source.chat_id, parts, OLDER_MESSAGE_ID)

    assert asyncio.run(run()).success
    assert_batch_contract(plugin, client.calls)
    assert [p for c in client.calls for p in c["parts"]] == plugin._fold_parts(parts)
    assert client.calls[0]["reply_to"] == {"message_id": OLDER_MESSAGE_ID}
    assert all(c["reply_to"] is None for c in client.calls[1:])
    assert len({c["idempotency_key"] for c in client.calls}) == len(client.calls)


@pytest.mark.parametrize("token", ["primary-relay-token", None])
def test_primary_multiplex_startup_uses_own_process_credentials(
    plugin, tmp_path, monkeypatch, token,
):
    from types import SimpleNamespace

    from agent import secret_scope as ss
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.run import GatewayRunner
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path / "relay"))
    monkeypatch.setenv("RELAY_ALLOWED_CONTACTS", "primary-contact")
    monkeypatch.setenv("RELAY_BASE_URL", "https://api.relayapp.im")
    if token is None:
        monkeypatch.delenv("RELAY_AGENT_TOKEN", raising=False)
    else:
        monkeypatch.setenv("RELAY_AGENT_TOKEN", token)
    home_token = set_hermes_home_override(str(tmp_path))
    secret_token = ss.set_secret_scope(None)
    was_multiplex = ss.is_multiplex_active()
    previous_entry = platform_registry.get("relayapp")
    ss.set_multiplex_active(True)
    try:
        plugin.register(SimpleNamespace(
            register_platform=lambda **kwargs: platform_registry.register(PlatformEntry(**kwargs)),
        ))
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        adapter = runner._create_adapter(Platform("relayapp"), PlatformConfig(enabled=True))
        if token is None:
            assert adapter is None
        else:
            assert adapter is not None
            assert adapter._token == token
            assert adapter._allowed_contacts == {"primary-contact"}
            assert adapter._inbox.path.parent == tmp_path / "relay"
        assert ss.current_secret_scope() is None
    finally:
        if previous_entry is not None:
            platform_registry.register(previous_entry)
        else:
            platform_registry.unregister("relayapp")
        ss.set_multiplex_active(was_multiplex)
        ss.reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
