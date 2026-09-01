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


def test_read_waits_until_hermes_processing_actually_starts(plugin, tmp_path):
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

    assert asyncio.run(adapter._on_inbound(inbound)) is True
    assert len(dispatched) == 1
    assert client.reads == []

    asyncio.run(adapter.on_processing_start(dispatched[0]))
    assert client.reads == [inbound.chat_id]


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
    assert calls[0]["allow_update_command"] is True


def test_env_enablement_keeps_chat_and_operator_allowlists_separate(
    plugin,
    monkeypatch,
):
    monkeypatch.setenv("RELAY_AGENT_TOKEN", "relay-test-token")
    monkeypatch.setenv("RELAY_ALLOWED_CONTACTS", "contact-one,contact-two")
    monkeypatch.setenv("RELAY_OPERATOR_CONTACTS", "contact-two")
    monkeypatch.delenv("RELAY_ALLOW_ALL_CONTACTS", raising=False)
    first = plugin._env_enablement()
    second = plugin._env_enablement()
    assert first["allowed_contacts"] == ["contact-one", "contact-two"]
    assert first["allowed_users"] == ["contact-one", "contact-two"]
    assert first["operator_contacts"] == ["contact-two"]
    assert first["allow_admin_from"] == ["contact-two"]
    assert first["group_allow_admin_from"] == ["contact-two"]
    assert second == first
    assert "RELAY_ALLOW_ALL_CONTACTS" not in os.environ


def test_ordinary_contacts_can_chat_but_are_not_gateway_operators(
    plugin,
    tmp_path,
    monkeypatch,
):
    import logging
    import types

    from gateway.authz_mixin import GatewayAuthorizationMixin
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.slash_access import policy_from_extra

    config = PlatformConfig(extra={
        "token": "relay-test-token",
        "state_dir": str(tmp_path),
        "allowed_contacts": ["ordinary-contact", "operator-contact"],
        "operator_contacts": ["operator-contact"],
    })
    adapter = plugin.RelayAdapter(config)

    assert adapter._allow_contact("ordinary-contact") is True
    assert adapter._allow_contact("operator-contact") is True
    assert config.extra["allowed_users"] == [
        "operator-contact",
        "ordinary-contact",
    ]

    runner = GatewayAuthorizationMixin()
    runner.config = GatewayConfig(platforms={adapter.platform: config})
    runner.adapters = {adapter.platform: adapter}
    runner._profile_adapters = {}
    runner.pairing_store = None
    gateway_run = types.ModuleType("gateway.run")
    gateway_run.logger = logging.getLogger("gateway.run")
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    source = adapter.build_source(
        chat_id="chat-id",
        chat_name="Relay Chat",
        chat_type="dm",
        user_id="ordinary-contact",
        user_name="Ordinary",
    )
    assert runner._is_user_authorized(source) is True
    source.user_id = "not-on-the-chat-allowlist"
    assert runner._is_user_authorized(source) is False

    dm_policy = policy_from_extra(config.extra, "dm")
    group_policy = policy_from_extra(config.extra, "group")
    assert dm_policy.enabled is True
    assert dm_policy.can_run("ordinary-contact", "update") is False
    assert group_policy.can_run("ordinary-contact", "update") is False
    assert dm_policy.can_run("operator-contact", "update") is True
    assert group_policy.can_run("operator-contact", "update") is True


def test_privileged_commands_default_deny_without_an_operator_decision(
    plugin,
    tmp_path,
):
    from gateway.config import PlatformConfig
    from gateway.slash_access import policy_from_extra

    config = PlatformConfig(extra={
        "token": "relay-test-token",
        "state_dir": str(tmp_path),
    })
    adapter = plugin.RelayAdapter(config)
    assert adapter._allow_contact("ordinary-contact") is True

    for scope in ("dm", "group"):
        policy = policy_from_extra(config.extra, scope)
        assert policy.enabled is True
        assert policy.can_run("ordinary-contact", "help") is True
        assert policy.can_run("ordinary-contact", "update") is False
        assert policy.can_run("ordinary-contact", "approvals") is False


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
    monkeypatch.setenv("RELAY_OPERATOR_CONTACTS", "process-operator")
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
                "RELAY_ALLOWED_CONTACTS": "a-contact,a-operator",
                "RELAY_OPERATOR_CONTACTS": "a-operator",
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
    assert profile_a._allowed_contacts == {"a-contact", "a-operator"}
    assert profile_a._operator_contacts == {"a-operator"}
    assert profile_a._inbox.path.parent == profile_a_state

    assert profile_b._token == "profile-b-token"
    assert profile_b._base_url == plugin.DEFAULT_BASE_URL
    assert profile_b._allowed_contacts == set()
    assert profile_b._operator_contacts == set()
    assert profile_b._inbox.path.parent == tmp_path / "profile-b" / "relay"
    assert config_b.extra["allowed_users"] == ["*"]
    assert config_b.extra["allow_admin_from"] == [
        plugin.NO_OPERATOR_CONTACT
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
