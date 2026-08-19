"""Adapter behavior under the split-at-ingest send contract.

One user turn must dispatch as one model turn even when the server splits it
into several ``message.received`` events, and one model turn must go out as
ONE POST whose parts the server commits as separate bubbles. These tests
drive the real adapter against a fake client, so they need the Hermes source
(the same gate ``test_env_enablement`` uses).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(
    os.environ.get("HERMES_AGENT_SRC", "").strip()
    or Path.home() / ".hermes/hermes-agent"
)

pytestmark = pytest.mark.skipif(
    not HERMES_SRC.is_dir(), reason="hermes source is not installed"
)


@pytest.fixture(scope="module")
def plugin():
    if str(HERMES_SRC) not in sys.path:
        sys.path.insert(0, str(HERMES_SRC))
    pkg_name = "hermes_relay_plugin"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg
    # ``Platform("relayapp")`` mints its pseudo-member only for registered
    # plugin names; outside a running gateway the registry starts empty.
    from gateway.platform_registry import PlatformEntry, platform_registry

    if not platform_registry.is_registered("relayapp"):
        platform_registry.register(
            PlatformEntry(
                name="relayapp",
                label="Relay",
                adapter_factory=lambda cfg: None,
                check_fn=lambda: True,
            )
        )
    return importlib.import_module(f"{pkg_name}.adapter")


class FakeClient:
    """Records every send and replays a fixed 202 body."""

    def __init__(self, body: Optional[Dict[str, Any]] = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.body = body if body is not None else {"messages": [{"id": "msg_head"}]}
        self._http = None

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
        self.calls.append(
            {
                "conversation_id": conversation_id,
                "parts": parts,
                "idempotency_key": idempotency_key,
                "reply_to": reply_to,
                "invocation_id": invocation_id,
            }
        )
        return self.body


def make_adapter(plugin, tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_STATE_DIR", raising=False)
    monkeypatch.delenv("RELAY_REPLY_TO_MODE", raising=False)
    from gateway.config import PlatformConfig

    config = PlatformConfig(
        extra={
            "token": "rly_live_test",
            "state_dir": str(tmp_path),
            "allow_all_users": "true",
        }
    )
    adapter = plugin.RelayAdapter(config)
    # Tight debounce so a coalescing test settles in milliseconds while the
    # merge logic under test stays the real one.
    adapter._text_batcher._batch_delay = 0.02
    adapter._text_batcher._split_delay = 0.02
    return adapter


def make_inbound(
    plugin,
    event_id: str,
    conversation_id: str,
    message_id: str,
    text: Optional[str] = None,
    media: bool = False,
    invocation_id: Optional[str] = None,
):
    api = importlib.import_module("hermes_relay_plugin.relay_api")
    parts: List[Dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    if media:
        parts.append({"type": "media", "url": "https://example.test/blob"})
    message = {
        "id": message_id,
        "conversation_id": conversation_id,
        "parts": parts,
        "sender": {"id": "usr_1", "kind": "user", "display_name": "Ada"},
        "created_at": "2026-08-19T00:00:00Z",
    }
    return api.InboundMessage(
        event={"event_id": event_id},
        event_id=event_id,
        message=message,
        conversation_id=conversation_id,
        message_id=message_id,
        sender_id="usr_1",
        sender_kind="user",
        invocation_id=invocation_id,
    )


def install_fake_ingest(adapter):
    async def fake_ingest(message):
        if any(
            isinstance(part, dict) and part.get("type") == "media"
            for part in message.get("parts") or []
        ):
            return ["/tmp/fake.jpg"], ["image/jpeg"], []
        return [], [], []

    adapter._ingest_media = fake_ingest


def capture_dispatches(adapter) -> List[Any]:
    dispatched: List[Any] = []

    async def record(event) -> None:
        dispatched.append(event)

    adapter._text_batcher._handler = record
    return dispatched


def require_pinned_hermes(plugin):
    """Skip when the installed hermes predates the plugin's README pin.

    ``_on_inbound`` builds a ``MessageEvent`` with ``user_id``/``user_name``,
    fields hermes gained on origin/main before the pinned ``e02d1e41``. An
    older source tree cannot construct the event at all, which says nothing
    about this plugin.
    """
    import dataclasses

    from gateway.platforms.base import MessageEvent

    if "user_id" not in {field.name for field in dataclasses.fields(MessageEvent)}:
        pytest.skip("installed hermes predates MessageEvent.user_id (plugin pins e02d1e41)")


# ---------------------------------------------------------------------------
# Inbound coalescing: one user turn, one model turn
# ---------------------------------------------------------------------------


def test_batcher_merges_media_into_the_pending_turn(plugin, tmp_path, monkeypatch):
    """The merge itself, below ``_on_inbound``, on any hermes generation."""
    from gateway.platforms.base import MessageEvent, MessageType

    adapter = make_adapter(plugin, tmp_path, monkeypatch)

    async def run():
        dispatched = capture_dispatches(adapter)
        adapter._text_batcher.enqueue(
            MessageEvent(text="look at this", message_type=MessageType.TEXT), "cnv_dm"
        )
        adapter._text_batcher.enqueue(
            MessageEvent(
                text=plugin.MEDIA_PLACEHOLDER,
                message_type=MessageType.PHOTO,
                media_urls=["/tmp/fake.jpg"],
                media_types=["image/jpeg"],
            ),
            "cnv_dm",
        )
        await asyncio.sleep(0.2)
        return dispatched

    dispatched = asyncio.run(run())
    assert len(dispatched) == 1
    merged = dispatched[0]
    assert merged.text == "look at this"
    assert merged.media_urls == ["/tmp/fake.jpg"]
    assert merged.media_types == ["image/jpeg"]
    assert merged.message_type == MessageType.PHOTO


def test_group_turn_is_one_post_and_consumes_the_invocation_once(plugin, tmp_path, monkeypatch):
    """The send half of the invocation contract, on any hermes generation."""
    import time as _time

    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    adapter._group_convs.add("cnv_grp")
    adapter._invocations["cnv_grp"] = ("inv_1", _time.time())

    async def run():
        first = await adapter.send("cnv_grp", "one\n\ntwo")
        second = await adapter.send("cnv_grp", "again")
        return first, second

    first, second = asyncio.run(run())
    assert first.success is True
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["invocation_id"] == "inv_1"
    assert call["parts"] == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]
    # The one reply consumed the invocation; a second group send has nothing
    # to attach and is suppressed rather than burned against the server.
    assert second.success is False


def test_split_text_plus_media_batch_dispatches_once(plugin, tmp_path, monkeypatch):
    require_pinned_hermes(plugin)
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    install_fake_ingest(adapter)

    async def run():
        dispatched = capture_dispatches(adapter)
        await adapter._on_inbound(
            make_inbound(plugin, "evt_1", "cnv_dm", "msg_1", text="look at this")
        )
        await adapter._on_inbound(
            make_inbound(plugin, "evt_2", "cnv_dm", "msg_2", media=True)
        )
        await asyncio.sleep(0.2)
        return dispatched

    dispatched = asyncio.run(run())
    assert len(dispatched) == 1
    event = dispatched[0]
    assert event.media_urls == ["/tmp/fake.jpg"]
    assert event.text == "look at this"


def test_media_first_batch_keeps_text_and_drops_placeholder(plugin, tmp_path, monkeypatch):
    require_pinned_hermes(plugin)
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    install_fake_ingest(adapter)

    async def run():
        dispatched = capture_dispatches(adapter)
        await adapter._on_inbound(
            make_inbound(plugin, "evt_1", "cnv_dm", "msg_1", media=True)
        )
        await adapter._on_inbound(
            make_inbound(plugin, "evt_2", "cnv_dm", "msg_2", text="thoughts?")
        )
        await asyncio.sleep(0.2)
        return dispatched

    dispatched = asyncio.run(run())
    assert len(dispatched) == 1
    assert dispatched[0].text == "thoughts?"
    assert dispatched[0].media_urls == ["/tmp/fake.jpg"]


def test_invoking_batch_coalesces_and_the_reply_consumes_once(plugin, tmp_path, monkeypatch):
    require_pinned_hermes(plugin)
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    install_fake_ingest(adapter)
    client = FakeClient()
    adapter._client = client

    async def run():
        dispatched = capture_dispatches(adapter)
        await adapter._on_inbound(
            make_inbound(plugin, "evt_1", "cnv_grp", "msg_1", text="hey", invocation_id="inv_1")
        )
        await adapter._on_inbound(
            make_inbound(plugin, "evt_2", "cnv_grp", "msg_2", media=True, invocation_id="inv_1")
        )
        await asyncio.sleep(0.2)
        first = await adapter.send("cnv_grp", "on it")
        second = await adapter.send("cnv_grp", "again")
        return dispatched, first, second

    dispatched, first, second = asyncio.run(run())
    assert len(dispatched) == 1
    assert len(client.calls) == 1
    assert client.calls[0]["invocation_id"] == "inv_1"
    assert first.success is True
    # The one reply consumed the invocation; a second group send has nothing
    # to attach and is suppressed rather than burned against the server.
    assert second.success is False


# ---------------------------------------------------------------------------
# Outbound: one turn is one POST, one part per bubble
# ---------------------------------------------------------------------------


def test_reply_paragraphs_ride_as_parts_of_one_post(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client

    result = asyncio.run(adapter.send("cnv_dm", "First.\n\nSecond.\n\nThird."))

    assert result.success is True
    assert result.message_id == "msg_head"
    assert len(client.calls) == 1
    assert client.calls[0]["parts"] == [
        {"type": "text", "text": "First."},
        {"type": "text", "text": "Second."},
        {"type": "text", "text": "Third."},
    ]


def test_reply_anchor_is_message_level_only(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    adapter._last_inbound["cnv_dm"] = "msg_newer"

    asyncio.run(adapter.send("cnv_dm", "answering the older one", reply_to="msg_older"))

    reply_to = client.calls[0]["reply_to"]
    assert reply_to == {"message_id": "msg_older"}
    assert "part_index" not in reply_to


def test_send_reads_the_legacy_202_shape_as_fallback(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    adapter._client = FakeClient(body={"message_id": "msg_legacy", "message": {"id": "msg_legacy"}})

    result = asyncio.run(adapter.send("cnv_dm", "hello"))

    assert result.success is True
    assert result.message_id == "msg_legacy"


def test_oversize_paragraph_parts_carry_no_chunk_indicators(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    wall = "word " * 4000  # ~20 KB, forces the length splitter

    asyncio.run(adapter.send("cnv_dm", wall))

    parts = client.calls[0]["parts"]
    assert len(parts) > 1
    for part in parts:
        assert len(part["text"].encode("utf-8")) <= plugin.MAX_MESSAGE_LENGTH
        assert not part["text"].rstrip().endswith(")") or "(1/" not in part["text"]


def test_image_batch_ships_as_contiguous_media_parts(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client

    asyncio.run(
        adapter.send_multiple_images(
            "cnv_dm",
            [
                ("https://example.test/a.png", ""),
                ("https://example.test/b.png", "the second one"),
            ],
        )
    )

    assert len(client.calls) == 1
    parts = client.calls[0]["parts"]
    assert parts[0] == {"type": "media", "url": "https://example.test/a.png"}
    assert parts[1] == {"type": "media", "url": "https://example.test/b.png"}
    assert parts[2] == {"type": "text", "text": "the second one"}


def test_caption_rides_after_the_media(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client

    asyncio.run(
        adapter.send_image("cnv_dm", "https://example.test/a.png", caption="sunset")
    )

    parts = client.calls[0]["parts"]
    assert parts == [
        {"type": "media", "url": "https://example.test/a.png"},
        {"type": "text", "text": "sunset"},
    ]
