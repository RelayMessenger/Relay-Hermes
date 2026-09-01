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
    return importlib.import_module("hermes_relay_plugin.adapter")


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
        self.voice_memos: List[Dict[str, str]] = []

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

    async def send_voice_memo(
        self,
        chat_id: str,
        attachment_id: str,
    ) -> Dict[str, Any]:
        self.voice_memos.append({
            "chat_id": chat_id,
            "attachment_id": attachment_id,
        })
        return {"voice_memo": {"id": "voice-message-id"}}


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
            reply_to=event.message_id,
        )
        second = await adapter.send(
            event.source.chat_id,
            "one\n\ntwo",
            reply_to=event.message_id,
        )
        # A processing retry starts the same logical turn from ordinal zero.
        await adapter.on_processing_start(event)
        retried = await adapter.send(
            event.source.chat_id,
            "one\n\ntwo",
            reply_to=event.message_id,
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
            {"type": "text", "value": "one"},
            {"type": "text", "value": "two"},
        ],
        "reply_to": {"message_id": event.message_id},
        "timeout": None,
    }
    assert {key: value for key, value in client.calls[0].items()
            if key != "idempotency_key"} == expected
    assert client.calls[0]["idempotency_key"] != client.calls[1]["idempotency_key"]
    assert client.calls[0]["idempotency_key"] == client.calls[2]["idempotency_key"]
    assert client.calls[0]["idempotency_key"] == "reply-event-send-0"
    assert client.calls[1]["idempotency_key"] == "reply-event-send-1"


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

    assert [inbound.sender_kind for inbound in received] == [sender_kind]
    assert adapter._inbox.status(payload["event_id"]) == "dispatched"
    adapter._inbox.close()


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

    assert adapter._direct_convs == {direct_chat}
    assert adapter._group_convs == {group_chat}
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
    assert len(folded) == 100
    assert folded[0] == {"type": "text", "value": "0"}
    assert folded[-1] == {"type": "text", "value": "99\n\n100"}

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
        {"type": "text", "value": "first"},
        {"type": "text", "value": "second"},
    ]


def test_audio_file_uses_attachment_upload_then_voice_memo_route(
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

    result = asyncio.run(adapter.send_voice("chat-id", str(audio)))
    assert result.success is True
    assert result.message_id == "voice-message-id"
    assert client.uploads == [{
        "filename": "memo.mp3",
        "content_type": "audio/mpeg",
        "data": b"audio",
    }]
    assert client.voice_memos == [{
        "chat_id": "chat-id",
        "attachment_id": "01993d50-ef7b-7b37-886b-23fd80c7ec16",
    }]


def test_unmentioned_group_event_is_not_dispatched(plugin, tmp_path):
    api = importlib.import_module("hermes_relay_plugin.relay_api")
    adapter = make_adapter(plugin, tmp_path)
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


def test_register_loads_as_a_hermes_platform_plugin(plugin):
    calls = []

    class Context:
        def register_platform(self, **kwargs):
            calls.append(kwargs)

    plugin.register(Context())
    assert len(calls) == 1
    assert calls[0]["name"] == "relayapp"
    assert calls[0]["required_env"] == ["RELAY_AGENT_TOKEN"]


def test_env_enablement_preserves_the_operator_allowlist(
    plugin,
    monkeypatch,
):
    monkeypatch.setenv("RELAY_AGENT_TOKEN", "relay-test-token")
    monkeypatch.setattr(plugin, "_OPERATOR_ALLOWED_USERS", "contact-one,contact-two")
    first = plugin._env_enablement()
    second = plugin._env_enablement()
    assert first["allowed_users"] == "contact-one,contact-two"
    assert second["allowed_users"] == "contact-one,contact-two"
    assert os.environ["RELAY_ALLOW_ALL_USERS"] == "true"
