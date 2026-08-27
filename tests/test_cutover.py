"""The cutover away from the deleted ``/responding`` route and the deleted
server-side invocation gate.

Three things are proven here. The client reaches read and typing through the
routes that are actually alive, under either name Relay has for a conversation,
so one build serves the server production runs today and the one it is being
cut over to. The mention gate answers the question the server used to answer:
in a group, is this agent being addressed. And an event still parses when its
message arrives under either shape.

No network: every call goes through a fake transport that answers only the
routes the named server really has.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from relay_api import (  # noqa: E402
    RelayApiError,
    RelayClient,
    RelayResponse,
    inbound_message_body,
    mentions_agent,
    parse_inbound,
)

TOKEN = "rly_live_test"


class DialectTransport:
    """A Relay that answers only the routes the named server really has.

    ``production`` is the deployed build, which knows ``/v1/conversations/...``
    and nothing else. ``live`` is the server after the rename, which knows
    ``/v1/chats/...`` and nothing else. ``bridged`` is the compatibility
    window, which answers both. Anything a server does not have answers 404,
    exactly as the real one does, so a build that guesses wrong is caught here.
    """

    def __init__(self, dialect: str, *, is_group: bool = False) -> None:
        self.dialect = dialect
        self.is_group = is_group
        self.calls: List[Dict[str, Any]] = []

    def paths(self) -> List[str]:
        return [call["path"] for call in self.calls]

    def _knows(self, path: str) -> bool:
        if path.startswith("/v1/chats/"):
            return self.dialect != "production"
        if path.startswith("/v1/conversations/"):
            return self.dialect != "live"
        return True

    async def __call__(self, **kwargs: Any) -> RelayResponse:
        self.calls.append(kwargs)
        path = kwargs["path"]
        if not self._knows(path):
            return RelayResponse(status=404, body={"error": {"code": "not_found"}})
        if path.endswith("/typing"):
            return RelayResponse(status=204, body=None)
        if path.endswith("/read"):
            return RelayResponse(status=200, body={"ok": True})
        if self.dialect == "production":
            return RelayResponse(status=200, body={
                "conversation": {"id": "cnv_1", "kind": "group" if self.is_group else "direct"},
            })
        return RelayResponse(status=200, body={"chat": {"id": "cnv_1", "is_group": self.is_group}})


def client_for(dialect: str, *, is_group: bool = False):
    transport = DialectTransport(dialect, is_group=is_group)
    return transport, RelayClient(TOKEN, transport=transport)


# ---------------------------------------------------------------------------
# The deleted route
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["production", "bridged", "live"])
def test_responding_route_is_never_called(dialect):
    transport, client = client_for(dialect)
    asyncio.run(client.mark_read("cnv_1", "msg_1"))
    asyncio.run(client.set_typing("cnv_1", True))
    assert not any("/responding" in path for path in transport.paths())


def test_the_responding_call_is_now_a_read_and_a_typing_call():
    transport, client = client_for("live")
    asyncio.run(client.mark_read("cnv_1", "msg_1"))
    asyncio.run(client.set_typing("cnv_1", True, label="Thinking"))
    assert transport.paths() == ["/v1/chats/cnv_1/read", "/v1/chats/cnv_1/typing"]
    assert transport.calls[0]["body"] == {"message_id": "msg_1"}
    assert transport.calls[1]["body"] == {"started": True, "label": "Thinking"}


def test_the_client_no_longer_offers_a_responding_call():
    # Keeping the method would leave a live caller pointed at a 404.
    assert not hasattr(RelayClient, "set_responding")


# ---------------------------------------------------------------------------
# Route tolerance
# ---------------------------------------------------------------------------


def test_every_route_reaches_the_server_production_runs_today():
    transport, client = client_for("production", is_group=True)
    asyncio.run(client.mark_read("cnv_1", "msg_1"))
    asyncio.run(client.set_typing("cnv_1", True, invocation_id="ivk_1"))
    assert asyncio.run(client.is_group_chat("cnv_1")) is True
    # Every old-name request is preceded by the live name being tried first,
    # and the fallback is what answers.
    assert transport.paths() == [
        "/v1/chats/cnv_1/read",
        "/v1/conversations/cnv_1/read",
        "/v1/chats/cnv_1/typing",
        "/v1/conversations/cnv_1/typing",
        "/v1/chats/cnv_1",
        "/v1/conversations/cnv_1",
    ]


def test_every_route_reaches_the_renamed_server_with_no_fallback_spent():
    transport, client = client_for("live", is_group=False)
    asyncio.run(client.mark_read("cnv_1", "msg_1"))
    asyncio.run(client.set_typing("cnv_1", False))
    assert asyncio.run(client.is_group_chat("cnv_1")) is False
    assert transport.paths() == [
        "/v1/chats/cnv_1/read",
        "/v1/chats/cnv_1/typing",
        "/v1/chats/cnv_1",
    ]


def test_the_bridged_server_takes_the_live_name():
    transport, client = client_for("bridged", is_group=True)
    asyncio.run(client.mark_read("cnv_1", "msg_1"))
    assert transport.paths() == ["/v1/chats/cnv_1/read"]


def test_the_group_flag_reads_under_either_name():
    for dialect in ("production", "live"):
        _, client = client_for(dialect, is_group=True)
        assert asyncio.run(client.is_group_chat("cnv_1")) is True
        _, dm = client_for(dialect, is_group=False)
        assert asyncio.run(dm.is_group_chat("cnv_1")) is False


def test_typing_still_forwards_an_invocation_that_arrived():
    # The live server ignores the field; the deployed one refuses group typing
    # without it, so forwarding what arrived is what keeps the typist visible.
    transport, client = client_for("live")
    asyncio.run(client.set_typing("cnv_1", True, invocation_id="ivk_1"))
    asyncio.run(client.set_typing("cnv_1", False))
    assert transport.calls[0]["body"] == {"started": True, "invocation_id": "ivk_1"}
    assert transport.calls[1]["body"] == {"started": False}


def test_a_status_that_is_not_404_is_not_retried_under_the_other_name():
    class Refusing:
        def __init__(self) -> None:
            self.calls: List[Dict[str, Any]] = []

        async def __call__(self, **kwargs: Any) -> RelayResponse:
            self.calls.append(kwargs)
            return RelayResponse(status=403, body={"error": {"code": "forbidden"}})

    transport = Refusing()
    client = RelayClient(TOKEN, transport=transport)
    with pytest.raises(RelayApiError):
        asyncio.run(client.mark_read("cnv_1", "msg_1"))
    # A refusal is an answer. Asking the same question under the old name
    # would double every rejected request.
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# The mention gate
# ---------------------------------------------------------------------------


HANDLE = "youragent"
AGENT_ID = "agt_1"


def mentioned_live() -> Dict[str, Any]:
    """A group message naming @youragent, in the live vocabulary."""
    return {
        "id": "msg_1",
        "conversation_id": "cnv_1",
        "parts": [{
            "type": "text",
            "text": "@youragent can you take this one?",
            "mention": "youragent",
            "mention_range": [0, 10],
        }],
    }


def mentioned_production() -> Dict[str, Any]:
    """The same message in the vocabulary the deployed server still speaks."""
    return {
        "id": "msg_1",
        "conversation_id": "cnv_1",
        "invoked_agents": ["agt_1"],
        "parts": [{
            "type": "text",
            "text": "@youragent can you take this one?",
            "mentions": [{"start": 0, "length": 10, "participant_id": "agt_1"}],
        }],
    }


def not_mentioned() -> Dict[str, Any]:
    return {
        "id": "msg_2",
        "conversation_id": "cnv_1",
        "parts": [{"type": "text", "text": "anyone up for lunch"}],
    }


def test_a_mention_is_seen_in_the_live_vocabulary():
    assert mentions_agent(mentioned_live(), handle=HANDLE, agent_id=AGENT_ID) is True


def test_a_mention_is_seen_in_the_deployed_servers_vocabulary():
    assert mentions_agent(mentioned_production(), handle=HANDLE, agent_id=AGENT_ID) is True


def test_a_message_that_names_nobody_is_not_a_mention():
    assert mentions_agent(not_mentioned(), handle=HANDLE, agent_id=AGENT_ID) is False


def test_a_message_that_names_another_agent_is_not_a_mention():
    other = {
        "id": "msg_3",
        "invoked_agents": ["agt_someone_else"],
        "parts": [{"type": "text", "text": "@scheduler move standup", "mention": "scheduler"}],
    }
    assert mentions_agent(other, handle=HANDLE, agent_id=AGENT_ID) is False


def test_the_handle_matches_however_a_client_cased_it():
    shouted = {"parts": [{"type": "text", "text": "@YourAgent hi", "mention": "@YourAgent"}]}
    assert mentions_agent(shouted, handle=HANDLE, agent_id=AGENT_ID) is True


def test_the_words_are_not_authority():
    # Relay's own rule: "Structured group targets. Text mentions are
    # presentation, never authority." A handle typed into a message that
    # carries no mention is talk about the agent, not to it — and reading it
    # as a summons is how an agent starts answering a conversation about it.
    talked_about = {"parts": [{"type": "text", "text": "@youragent is pretty good tbh"}]}
    assert mentions_agent(talked_about, handle=HANDLE, agent_id=AGENT_ID) is False


def test_an_unknown_identity_names_nobody_rather_than_guessing():
    assert mentions_agent(mentioned_live()) is False


def test_a_mention_hung_on_a_part_that_is_not_text_is_ignored():
    on_media = {"parts": [{"type": "media", "mention": "youragent"}]}
    assert mentions_agent(on_media, handle=HANDLE, agent_id=AGENT_ID) is False


def test_a_message_with_no_parts_names_nobody():
    assert mentions_agent({}, handle=HANDLE, agent_id=AGENT_ID) is False
    assert mentions_agent({"parts": None}, handle=HANDLE, agent_id=AGENT_ID) is False


# ---------------------------------------------------------------------------
# What an event alone can say about a group
# ---------------------------------------------------------------------------


def event_for(message: Dict[str, Any], *, invocation_id: Optional[str] = None,
              wrapped: bool = True) -> Dict[str, Any]:
    data: Dict[str, Any] = {"message": message} if wrapped else dict(message)
    if invocation_id:
        data["invocation_id"] = invocation_id
    return {"event_id": "evt_1", "event_type": "message.received", "data": data}


def test_an_invocation_still_proves_a_group():
    inbound = parse_inbound(event_for(mentioned_live(), invocation_id="ivk_1"))
    assert inbound is not None
    assert inbound.group_hint is True
    assert inbound.invocation_id == "ivk_1"


def test_no_invocation_proves_nothing_rather_than_proving_a_dm():
    # This is the whole defect. Reading the absence as "not a group" is what
    # would make every group look like a DM once the server stopped minting.
    inbound = parse_inbound(event_for(not_mentioned()))
    assert inbound is not None
    assert inbound.group_hint is None
    assert inbound.invocation_id is None


def test_the_message_parses_under_either_event_shape():
    wrapped = parse_inbound(event_for(mentioned_live(), wrapped=True))
    unwrapped = parse_inbound(event_for(mentioned_live(), wrapped=False))
    assert wrapped is not None and unwrapped is not None
    assert wrapped.message_id == unwrapped.message_id == "msg_1"
    assert wrapped.conversation_id == unwrapped.conversation_id == "cnv_1"
    assert mentions_agent(unwrapped.message, handle=HANDLE, agent_id=AGENT_ID) is True


def test_the_conversation_id_reads_under_either_name():
    message = {"id": "msg_9", "chat_id": "cnv_9", "parts": []}
    inbound = parse_inbound(event_for(message, wrapped=False))
    assert inbound is not None
    assert inbound.conversation_id == "cnv_9"


def test_an_event_carrying_no_message_is_still_ignored():
    assert inbound_message_body({"data": {}}) is None
    assert parse_inbound({"event_type": "message.received", "data": {}}) is None
    assert parse_inbound({"event_type": "message.read", "data": {"id": "msg_1"}}) is None
