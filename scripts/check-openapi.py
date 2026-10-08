#!/usr/bin/env python3
"""Verify the exact Relay OpenAPI used by this release candidate."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import yaml

RELAY_OPENAPI_COMMIT = "6f50fcb69d1ce6dde8bf0fb0e12bd2ef379f0b09"
RELAY_OPENAPI_SHA256 = (
    "79bd85b0150ef45ea4bbe5f498507dd86d784e7fbf6c3b299a5a81db091aacdd"
)


def check_openapi(path: Path) -> None:
    raw = path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != RELAY_OPENAPI_SHA256:
        raise AssertionError(
            f"OpenAPI SHA-256 mismatch: expected {RELAY_OPENAPI_SHA256}, "
            f"received {actual_hash}"
        )

    spec = yaml.safe_load(raw)
    paths = spec["paths"]
    required_paths = {
        "/v1/chats",
        "/v1/chats/{chatId}/messages",
        "/v1/chats/{chatId}/read",
        "/v1/attachments",
        "/v1/websocket",
        "/v1/chats/{chatId}/location/request",
        "/v1/chats/{chatId}/location",
    }
    assert required_paths <= paths.keys()
    assert "post" not in paths.get("/v1/agents", {}), "Anonymous Agent registration is retired"
    assert "/v1/events" not in paths
    assert "/v1/conversations" not in paths

    send = paths["/v1/chats/{chatId}/messages"]["post"]
    assert any(
        parameter["name"] == "Idempotency-Key"
        for parameter in send["parameters"]
    )
    request_schema = send["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["$ref"].endswith("/SendMessageToChatRequest")

    read = paths["/v1/chats/{chatId}/read"]["post"]
    assert "requestBody" not in read
    assert read["responses"]["204"]["description"]

    websocket = paths["/v1/websocket"]["get"]
    assert websocket["operationId"] == "connectAgentWebSocket"
    frames = set(spec["x-relay-websocket-frames"])
    assert frames == {
        "ready",
        "event",
        "ack",
        "full_sync",
        "full_sync_complete",
        "ping",
        "pong",
        "error",
        "disconnect",
    }

    schemas = spec["components"]["schemas"]
    assert {
        "Chat",
        "ChatHandle",
        "Message",
        "ParticipantAddedEvent",
        "ParticipantRemovedEvent",
    } <= schemas.keys()
    envelope = schemas["WebhookEnvelopeBase"]["properties"]
    assert envelope["api_version"]["enum"] == ["v1"]
    assert [
        str(value) for value in envelope["webhook_version"]["enum"]
    ] == ["2026-08-30"]
    assert {
        "contact.added",
        "contact.removed",
    } <= set(schemas["WebhookEventType"]["enum"])

    # The parts the plugin sends and the system event it reads.
    assert "post" in paths["/v1/chats/{chatId}/location/request"]
    assert "get" in paths["/v1/chats/{chatId}/location"]
    for name, part_type in (
        ("PlacePart", "place"),
        ("FormPart", "form"),
        ("RichCardPart", "rich_card"),
        ("CarouselPart", "carousel"),
        ("RatingRequestPart", "rating_request"),
    ):
        assert schemas[name]["properties"]["type"]["enum"] == [part_type], name
    system_event = schemas["SystemEvent"]["properties"]
    assert "contact_card_shared" in system_event["type"]["enum"]
    assert "contact_card" in system_event

    print(
        "openapi_contract=pass "
        f"commit={RELAY_OPENAPI_COMMIT} "
        f"sha256={RELAY_OPENAPI_SHA256} "
        f"required_paths={len(required_paths)} "
        f"websocket_frames={len(frames)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("spec", type=Path)
    arguments = parser.parse_args()
    check_openapi(arguments.spec)


if __name__ == "__main__":
    main()
