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
        self.typing_calls: List[Dict[str, Any]] = []
        self.body = body if body is not None else {"messages": [{"id": "msg_head"}]}
        self._http = None

    async def set_typing(
        self,
        conversation_id: str,
        started: bool,
        *,
        label: Optional[str] = None,
        invocation_id: Optional[str] = None,
        timeout: Optional[float] = 5.0,
    ) -> None:
        self.typing_calls.append(
            {
                "conversation_id": conversation_id,
                "started": started,
                "invocation_id": invocation_id,
            }
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


def test_group_turn_is_one_post_and_forwards_the_invocation_once(plugin, tmp_path, monkeypatch):
    """A group turn is ONE POST, and an invocation that arrived rides with it."""
    import time as _time

    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    adapter._group_convs.add("cnv_grp")
    adapter._invocations["cnv_grp"] = [("inv_1", _time.time())]

    result = asyncio.run(adapter.send("cnv_grp", "one\n\ntwo"))
    assert result.success is True
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["invocation_id"] == "inv_1"
    assert call["parts"] == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]
    # A landed send drops the id, so a later send does not attach a spent one.
    assert "cnv_grp" not in adapter._invocations


def test_a_group_send_with_no_invocation_still_goes_out(plugin, tmp_path, monkeypatch):
    """The suppression this replaced would now silence every group reply.

    ``_commit_group_message`` used to refuse a send with no pending
    invocation, because the server would have refused it too. The server no
    longer mints or checks one, so a missing invocation is the ordinary case.
    Whether the agent should speak at all is decided once, at the mention gate,
    before the model ever runs.
    """
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    adapter._group_convs.add("cnv_grp")
    assert not adapter._invocations

    result = asyncio.run(adapter.send("cnv_grp", "here you go"))
    assert result.success is True
    assert len(client.calls) == 1
    assert client.calls[0]["invocation_id"] is None


def test_merged_dm_batch_answers_the_newest_fragment_without_a_quote(plugin, tmp_path, monkeypatch):
    """A split user send in a DM must not earn a quote in auto reply mode.

    The merge must carry the LAST fragment's id: ``_last_inbound`` holds the
    last id, so a merged batch keeping the first would make ``auto`` quote
    every split send, exactly what it documents it will not do.
    """
    from gateway.platforms.base import MessageEvent, MessageType

    adapter = make_adapter(plugin, tmp_path, monkeypatch)

    async def run():
        dispatched = capture_dispatches(adapter)
        adapter._text_batcher.enqueue(
            MessageEvent(text="part one", message_type=MessageType.TEXT, message_id="msg_1"),
            "cnv_dm",
        )
        adapter._text_batcher.enqueue(
            MessageEvent(text="part two", message_type=MessageType.TEXT, message_id="msg_2"),
            "cnv_dm",
        )
        await asyncio.sleep(0.2)
        return dispatched

    dispatched = asyncio.run(run())
    assert len(dispatched) == 1
    merged = dispatched[0]
    assert merged.message_id == "msg_2"
    adapter._last_inbound["cnv_dm"] = "msg_2"
    # auto mode: answering the newest message needs no quote.
    assert adapter._reply_anchor("cnv_dm", merged.message_id) is None


def test_overlapping_invocations_get_their_own_dispatch_and_consumption(plugin, tmp_path, monkeypatch):
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
            make_inbound(plugin, "evt_2", "cnv_grp", "msg_2", text="also hey", invocation_id="inv_2")
        )
        await asyncio.sleep(0.2)
        first = await adapter.send("cnv_grp", "answering the first")
        second = await adapter.send("cnv_grp", "answering the second")
        return dispatched, first, second

    dispatched, first, second = asyncio.run(run())
    # Two invocations inside one window are two turns, not one merged turn.
    assert len(dispatched) == 2
    assert first.success is True
    assert second.success is True
    assert [call["invocation_id"] for call in client.calls] == ["inv_1", "inv_2"]
    assert "cnv_grp" not in adapter._invocations


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


def test_forty_paragraph_reply_folds_into_the_part_cap_without_loss(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    paragraphs = [f"Thought number {index}." for index in range(40)]
    content = "\n\n".join(paragraphs)

    result = asyncio.run(adapter.send("cnv_dm", content))

    assert result.success is True
    parts = client.calls[0]["parts"]
    # More than 32 parts is a non-retryable 422 and the whole reply drops;
    # trailing paragraphs rejoin instead, losing nothing.
    assert len(parts) == plugin.MAX_PARTS_PER_POST
    assert "\n\n".join(part["text"] for part in parts) == content


def test_image_batch_folds_trailing_alt_texts_into_the_part_cap(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    images = [(f"https://example.test/{index}.png", f"alt {index}") for index in range(20)]

    asyncio.run(adapter.send_multiple_images("cnv_dm", images))

    parts = client.calls[0]["parts"]
    assert len(parts) == plugin.MAX_PARTS_PER_POST
    assert sum(1 for part in parts if part["type"] == "media") == 20
    joined = "\n\n".join(part["text"] for part in parts if part["type"] == "text")
    assert joined == "\n\n".join(f"alt {index}" for index in range(20))


def test_heavy_paragraphs_fold_within_the_byte_budget(plugin, tmp_path, monkeypatch):
    """40 x 1000-byte paragraphs: the fold must move to earlier seams
    instead of growing one part past the server's 8192-byte cap."""
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    paragraphs = [chr(ord("a") + index % 26) * 1000 for index in range(40)]
    content = "\n\n".join(paragraphs)

    result = asyncio.run(adapter.send("cnv_dm", content))

    assert result.success is True
    assert len(client.calls) == 1
    parts = client.calls[0]["parts"]
    assert len(parts) <= plugin.MAX_PARTS_PER_POST
    for part in parts:
        assert len(part["text"].encode("utf-8")) <= plugin.MERGE_BYTE_BUDGET
    assert "\n\n".join(part["text"] for part in parts) == content


def test_dm_media_overflow_ships_as_successive_posts(plugin, tmp_path, monkeypatch):
    """Media cannot fold, so a 40-image DM batch becomes two ordered POSTs."""
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    images = [(f"https://example.test/{index}.png", "") for index in range(40)]

    asyncio.run(adapter.send_multiple_images("cnv_dm", images))

    assert len(client.calls) == 2
    assert len(client.calls[0]["parts"]) == plugin.MAX_PARTS_PER_POST
    assert len(client.calls[1]["parts"]) == 8
    urls = [
        part["url"] for call in client.calls for part in call["parts"]
    ]
    assert urls == [f"https://example.test/{index}.png" for index in range(40)]
    assert client.calls[0]["idempotency_key"] != client.calls[1]["idempotency_key"]


def test_group_media_overflow_keeps_the_first_32_and_warns(plugin, tmp_path, monkeypatch, caplog):
    """A group turn is one POST: the overflow drops with a logged count,
    because a second POST cannot ride the consumed invocation."""
    import logging
    import time as _time

    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    adapter._group_convs.add("cnv_grp")
    adapter._invocations["cnv_grp"] = [("inv_1", _time.time())]
    images = [(f"https://example.test/{index}.png", "") for index in range(40)]

    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter.send_multiple_images("cnv_grp", images))

    assert len(client.calls) == 1
    parts = client.calls[0]["parts"]
    assert len(parts) == plugin.MAX_PARTS_PER_POST
    assert parts[-1]["url"] == "https://example.test/31.png"
    assert client.calls[0]["invocation_id"] == "inv_1"
    assert any("dropping 8 part(s)" in record.getMessage() for record in caplog.records)


def test_group_overflow_keeps_the_caption_over_a_photo(plugin, tmp_path, monkeypatch, caplog):
    """Words survive ahead of pictures when a group turn will not fit.

    A caption rides after its media, so a plain prefix would keep 32 photos
    and silently lose the sentence describing them. Dropping a photo is
    visible; dropping the words is not.
    """
    import logging
    import time as _time

    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    client = FakeClient()
    adapter._client = client
    adapter._group_convs.add("cnv_grp")
    adapter._invocations["cnv_grp"] = [("inv_1", _time.time())]
    # 33 photos plus one alt text: the alt rides AFTER its media, so a plain
    # prefix of 32 would keep every photo and drop the only words.
    images = [(f"https://example.test/{index}.png", "") for index in range(32)]
    images.append(("https://example.test/32.png", "here they are"))

    with caplog.at_level(logging.WARNING):
        asyncio.run(adapter.send_multiple_images("cnv_grp", images))

    assert len(client.calls) == 1
    parts = client.calls[0]["parts"]
    assert len(parts) == plugin.MAX_PARTS_PER_POST
    texts = [part for part in parts if part["type"] == "text"]
    assert any(part["text"] == "here they are" for part in texts), parts
    assert any("0 of them text" in record.getMessage() for record in caplog.records)


def test_out_of_order_group_replies_bind_to_their_own_invocation(plugin, tmp_path, monkeypatch):
    require_pinned_hermes(plugin)
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    install_fake_ingest(adapter)
    client = FakeClient()
    adapter._client = client

    async def run():
        capture_dispatches(adapter)
        await adapter._on_inbound(
            make_inbound(plugin, "evt_1", "cnv_grp", "msg_1", text="first ask", invocation_id="inv_1")
        )
        await adapter._on_inbound(
            make_inbound(plugin, "evt_2", "cnv_grp", "msg_2", text="second ask", invocation_id="inv_2")
        )
        await asyncio.sleep(0.2)
        # The second turn finishes first: its reply anchors msg_2 and must
        # carry inv_2, not the FIFO head inv_1.
        late = await adapter.send("cnv_grp", "answering the second", reply_to="msg_2")
        early = await adapter.send("cnv_grp", "answering the first", reply_to="msg_1")
        return late, early

    late, early = asyncio.run(run())
    assert late.success is True
    assert early.success is True
    assert [call["invocation_id"] for call in client.calls] == ["inv_2", "inv_1"]
    assert "cnv_grp" not in adapter._invocations


def test_a_turn_carries_its_invocation_to_sends_with_no_anchor(plugin, tmp_path, monkeypatch):
    """Two group turns finish out of order and neither send has a reply anchor.

    An image batch and a typing signal both go out with no ``reply_to``, so
    the anchor cannot say which turn they belong to. The dispatch context
    must: hermes runs each turn in its own task and asyncio copies the
    context into it.
    """
    require_pinned_hermes(plugin)
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    install_fake_ingest(adapter)
    client = FakeClient()
    adapter._client = client
    turns: List[Any] = []

    async def fake_handle(event):
        async def turn(ev):
            if ev.text == "first ask":
                # The turn invoked FIRST answers LAST.
                await asyncio.sleep(0.2)
            await adapter.send_typing(ev.source.chat_id)
            await adapter.send_multiple_images(
                ev.source.chat_id, [("https://example.test/a.png", "")]
            )

        turns.append(asyncio.create_task(turn(event)))

    adapter.handle_message = fake_handle

    async def run():
        await adapter._on_inbound(
            make_inbound(plugin, "evt_1", "cnv_grp", "msg_1", text="first ask", invocation_id="inv_1")
        )
        await adapter._on_inbound(
            make_inbound(plugin, "evt_2", "cnv_grp", "msg_2", text="second ask", invocation_id="inv_2")
        )
        await asyncio.sleep(0.1)
        await asyncio.gather(*turns)

    asyncio.run(run())

    assert [call["invocation_id"] for call in client.calls] == ["inv_2", "inv_1"]
    assert [call["reply_to"] for call in client.calls] == [None, None]
    assert [call["invocation_id"] for call in client.typing_calls] == ["inv_2", "inv_1"]
    assert "cnv_grp" not in adapter._invocations


def test_malformed_202_body_returns_a_result_instead_of_raising(plugin, tmp_path, monkeypatch):
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    adapter._client = FakeClient(body={"messages": 7})

    result = asyncio.run(adapter.send("cnv_dm", "hello"))

    assert result.success is False
    assert result.error


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


# ---------------------------------------------------------------------------
# The group gate, end to end through _on_inbound
#
# Relay used to answer this itself, by delivering a group agent only the
# messages it had been invoked on. It no longer does, so these drive the real
# handler and assert what reaches the model.
# ---------------------------------------------------------------------------


class GroupKindClient(FakeClient):
    """A FakeClient that also answers what kind of chat this is."""

    def __init__(self, groups=(), **kwargs):
        super().__init__(**kwargs)
        self._groups = set(groups)
        self.chat_lookups: List[str] = []

    async def is_group_chat(self, conversation_id: str) -> bool:
        self.chat_lookups.append(conversation_id)
        return conversation_id in self._groups


def mention_inbound(plugin, conversation_id, message_id, *, mention=None,
                    invoked=None, text="hello"):
    """One inbound message, optionally naming an agent."""
    api = importlib.import_module("hermes_relay_plugin.relay_api")
    part: Dict[str, Any] = {"type": "text", "text": text}
    if mention:
        part["mention"] = mention
    message: Dict[str, Any] = {
        "id": message_id,
        "conversation_id": conversation_id,
        "parts": [part],
        "sender": {"id": "usr_1", "kind": "user", "display_name": "Ada"},
        "created_at": "2026-08-27T00:00:00Z",
    }
    if invoked:
        message["invoked_agents"] = list(invoked)
    return api.InboundMessage(
        event={"event_id": message_id},
        event_id=message_id,
        message=message,
        conversation_id=conversation_id,
        message_id=message_id,
        sender_id="usr_1",
        sender_kind="user",
    )


def gate_adapter(plugin, tmp_path, monkeypatch, *, groups=("cnv_grp",), policy=None):
    monkeypatch.delenv("RELAY_GROUP_REPLY_POLICY", raising=False)
    if policy is not None:
        monkeypatch.setenv("RELAY_GROUP_REPLY_POLICY", policy)
    adapter = make_adapter(plugin, tmp_path, monkeypatch)
    adapter._client = GroupKindClient(groups=groups)
    adapter._agent_handle = "youragent"
    adapter._agent_id = "agt_1"
    install_fake_ingest(adapter)
    return adapter, capture_dispatches(adapter)


def test_a_group_message_naming_nobody_never_reaches_the_model(plugin, tmp_path, monkeypatch):
    # No pin guard: the gate returns before a MessageEvent is built, so the
    # silence half of the contract is provable on any hermes generation.
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch)

    async def run():
        await adapter._on_inbound(
            mention_inbound(plugin, "cnv_grp", "msg_1", text="anyone up for lunch")
        )
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert dispatched == []


def test_a_group_message_naming_the_agent_reaches_the_model(plugin, tmp_path, monkeypatch):
    require_pinned_hermes(plugin)
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch)

    async def run():
        await adapter._on_inbound(
            mention_inbound(
                plugin, "cnv_grp", "msg_1",
                mention="youragent", text="@youragent take this one",
            )
        )
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert len(dispatched) == 1
    assert dispatched[0].source.chat_type == "group"


def test_the_agent_id_dialect_also_names_the_agent(plugin, tmp_path, monkeypatch):
    """The vocabulary the deployed server still speaks: invoked_agents."""
    require_pinned_hermes(plugin)
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch)

    async def run():
        await adapter._on_inbound(
            mention_inbound(plugin, "cnv_grp", "msg_1", invoked=["agt_1"])
        )
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert len(dispatched) == 1


def test_a_direct_message_reaches_the_model_with_no_mention(plugin, tmp_path, monkeypatch):
    require_pinned_hermes(plugin)
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch, groups=())

    async def run():
        await adapter._on_inbound(mention_inbound(plugin, "cnv_dm", "msg_1"))
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert len(dispatched) == 1
    assert dispatched[0].source.chat_type == "dm"


def test_the_all_policy_lets_every_group_message_through(plugin, tmp_path, monkeypatch):
    require_pinned_hermes(plugin)
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch, policy="all")

    async def run():
        await adapter._on_inbound(
            mention_inbound(plugin, "cnv_grp", "msg_1", text="anyone up for lunch")
        )
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert len(dispatched) == 1


def test_a_typo_in_the_policy_does_not_open_the_floodgate(plugin, tmp_path, monkeypatch):
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch, policy="ALL_OF_THEM")

    async def run():
        await adapter._on_inbound(
            mention_inbound(plugin, "cnv_grp", "msg_1", text="anyone up for lunch")
        )
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert adapter._group_reply_policy == "mentions"
    assert dispatched == []


def test_the_chat_kind_is_read_once_and_remembered(plugin, tmp_path, monkeypatch):
    """A thread never changes kind, so it costs one lookup for its lifetime.

    Driven through unnamed group messages so no MessageEvent is built and the
    caching is provable on any hermes generation.
    """
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch)

    async def run():
        for index in range(3):
            await adapter._on_inbound(
                mention_inbound(plugin, "cnv_grp", f"msg_{index}", text="lunch?")
            )
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert adapter._client.chat_lookups == ["cnv_grp"]
    assert dispatched == []


def test_a_chat_lookup_that_fails_routes_into_the_gate_rather_than_past_it(
    plugin, tmp_path, monkeypatch
):
    """Unknown kind is treated as a group, so silence is the safe side.

    A lookup that fails leaves the kind unknown. Answering anyway is how an
    agent starts talking in a room it was never addressed in, so the unknown
    case goes through the mention gate rather than around it.
    """
    adapter, dispatched = gate_adapter(plugin, tmp_path, monkeypatch, groups=())

    async def explode(conversation_id: str) -> bool:
        raise RuntimeError("relay is unreachable")

    adapter._client.is_group_chat = explode

    async def run():
        await adapter._on_inbound(
            mention_inbound(plugin, "cnv_unknown", "msg_1", text="anyone up for lunch")
        )
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert dispatched == []
    # Nothing was cached, so a later read can still learn the real kind.
    assert "cnv_unknown" not in adapter._group_convs
    assert "cnv_unknown" not in adapter._direct_convs
