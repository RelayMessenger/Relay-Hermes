#!/usr/bin/env python3
"""Verify the exact Relay OpenAPI used by this release candidate."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import yaml

RELAY_OPENAPI_COMMIT = "9b4d5bb32cc749c6fd271969948c385300d404d6"
RELAY_OPENAPI_SHA256 = (
    "f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5"
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
    }
    assert required_paths <= paths.keys()
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
