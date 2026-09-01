from __future__ import annotations

import json
import os
import shutil
import sqlite3

import pytest

from relay_hermes.state import (
    STATE_BINDING_FILENAME,
    STATE_DIRECTORY_MODE,
    RelayInbox,
    RelayStateBinding,
    RelayStateBindingError,
)

ORIGIN = "https://api.staging.relayapp.im"
TOKEN = "relay-test-token"


def open_inbox(path, *, origin=ORIGIN, token=TOKEN):
    return RelayInbox(
        path,
        binding=RelayStateBinding.for_account(origin, token),
    ).open()


def payload(event_id: str):
    return {
        "event_id": event_id,
        "event_type": "message.received",
        "data": {"id": "message"},
    }


def snapshot():
    return [
        {
            "chat": {
                "id": "01993d50-ef7b-7b37-886b-23fd80c7ec10",
                "is_group": False,
            },
            "messages": [
                {
                    "id": "01993d50-ef7b-7b37-886b-23fd80c7ec12",
                    "chat_id": "01993d50-ef7b-7b37-886b-23fd80c7ec10",
                    "is_from_me": True,
                },
                {
                    "id": "01993d50-ef7b-7b37-886b-23fd80c7ec11",
                    "chat_id": "01993d50-ef7b-7b37-886b-23fd80c7ec10",
                    "is_from_me": False,
                },
            ],
        },
        {
            "chat": {
                "id": "01993d50-ef7b-7b37-886b-23fd80c7ec20",
                "is_group": True,
            },
            "messages": [],
        },
    ]


def test_accept_is_durable_and_deduplicates_event_id(tmp_path):
    path = tmp_path / "relay" / "inbox.sqlite3"
    inbox = open_inbox(path)
    assert inbox.accept("1", payload("event-1")) is True
    assert inbox.accept("2", payload("event-1")) is False
    assert inbox.event_count() == 1
    inbox.close()

    reopened = open_inbox(path)
    assert reopened.status("event-1") == "pending"
    event_id, event = reopened.next_pending()
    assert event_id == "event-1"
    assert event == payload("event-1")
    reopened.complete(event_id)
    assert reopened.status(event_id) == "completed"
    reopened.close()


def test_failed_processing_returns_to_pending(tmp_path):
    inbox = open_inbox(tmp_path / "inbox.sqlite3")
    inbox.accept("9", payload("event-9"))
    event_id, _ = inbox.next_pending()
    assert inbox.status(event_id) == "processing"
    inbox.retry(event_id, "model unavailable")
    assert inbox.status(event_id) == "pending"
    inbox.close()


def test_dispatched_work_is_not_selected_twice_and_recovers_after_restart(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = open_inbox(path)
    inbox.accept("10", payload("event-10"))
    event_id, _ = inbox.next_pending()
    inbox.mark_dispatched(event_id)
    assert inbox.next_pending() is None
    inbox.close()

    recovered = open_inbox(path)
    assert recovered.status(event_id) == "pending"
    assert recovered.next_pending()[0] == event_id
    recovered.close()


def test_sequence_and_event_id_are_validated(tmp_path):
    inbox = open_inbox(tmp_path / "inbox.sqlite3")
    with pytest.raises(ValueError):
        inbox.accept("-1", payload("event"))
    with pytest.raises(ValueError):
        inbox.accept("01", payload("event"))
    with pytest.raises(ValueError):
        inbox.accept("1", {})
    inbox.accept("1", payload("event-1"))
    with pytest.raises(ValueError, match="different event_id"):
        inbox.accept("1", payload("event-2"))
    assert inbox.event_count() == 1
    inbox.close()


def test_full_snapshot_and_checkpoint_are_atomic_durable_and_deterministic(
    tmp_path,
):
    path = tmp_path / "inbox.sqlite3"
    inbox = open_inbox(path)
    first = snapshot()
    digest = inbox.replace_full_snapshot("42", first)
    assert inbox.full_sync_state() == {
        "through_sequence": "42",
        "snapshot_sha256": digest,
        "chat_count": 2,
        "message_count": 2,
    }

    reordered = [first[1], {
        "chat": first[0]["chat"],
        "messages": list(reversed(first[0]["messages"])),
    }]
    assert inbox.replace_full_snapshot("43", reordered) == digest
    inbox.close()

    reopened = open_inbox(path)
    restored = reopened.load_full_snapshot()
    assert [entry["chat"]["id"] for entry in restored] == sorted(
        entry["chat"]["id"] for entry in first
    )
    assert [
        message["id"] for message in restored[0]["messages"]
    ] == sorted(message["id"] for message in first[0]["messages"])
    assert reopened.full_sync_state()["through_sequence"] == "43"
    reopened.close()


def test_invalid_full_snapshot_does_not_replace_last_good_checkpoint(tmp_path):
    inbox = open_inbox(tmp_path / "inbox.sqlite3")
    expected = snapshot()
    inbox.replace_full_snapshot("9", expected)
    invalid = snapshot()
    invalid[0]["messages"][0]["chat_id"] = "wrong-chat"

    with pytest.raises(ValueError, match="invalid Message"):
        inbox.replace_full_snapshot("10", invalid)

    assert inbox.full_sync_state()["through_sequence"] == "9"
    assert inbox.load_full_snapshot() == [
        {
            "chat": expected[0]["chat"],
            "messages": sorted(
                expected[0]["messages"],
                key=lambda message: message["id"],
            ),
        },
        expected[1],
    ]
    inbox.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode")
def test_inbox_is_owner_only(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    open_inbox(path).close()
    assert os.stat(path.parent).st_mode & 0o777 == STATE_DIRECTORY_MODE
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent / STATE_BINDING_FILENAME).st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory mode")
def test_preexisting_permissive_state_directory_is_forced_to_owner_only(tmp_path):
    state_dir = tmp_path / "relay"
    path = state_dir / "inbox.sqlite3"
    open_inbox(path).close()
    os.chmod(state_dir, 0o777)

    open_inbox(path).close()

    assert os.stat(state_dir).st_mode & 0o777 == STATE_DIRECTORY_MODE


@pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow directory open")
def test_state_directory_symlink_is_refused_without_chmodding_target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    os.chmod(target, 0o777)
    state_dir = tmp_path / "relay"
    state_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(RelayStateBindingError, match="real directory"):
        open_inbox(state_dir / "inbox.sqlite3")

    assert os.stat(target).st_mode & 0o777 == 0o777
    assert list(target.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity")
def test_replacement_during_directory_chmod_is_detected_without_touching_replacement(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / "relay"
    state_dir.mkdir()
    os.chmod(state_dir, 0o777)
    displaced = tmp_path / "relay-displaced"
    real_fchmod = os.fchmod
    replaced = False

    def replace_after_fchmod(descriptor, mode):
        nonlocal replaced
        real_fchmod(descriptor, mode)
        if mode == STATE_DIRECTORY_MODE and not replaced:
            replaced = True
            state_dir.rename(displaced)
            state_dir.mkdir()
            os.chmod(state_dir, 0o777)

    monkeypatch.setattr(os, "fchmod", replace_after_fchmod)

    with pytest.raises(RelayStateBindingError, match="replaced during use"):
        open_inbox(state_dir / "inbox.sqlite3")

    assert os.stat(displaced).st_mode & 0o777 == STATE_DIRECTORY_MODE
    assert os.stat(state_dir).st_mode & 0o777 == 0o777
    assert list(state_dir.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity")
def test_replacement_after_open_blocks_state_access(tmp_path):
    state_dir = tmp_path / "relay"
    path = state_dir / "inbox.sqlite3"
    inbox = open_inbox(path)
    displaced = tmp_path / "relay-displaced"
    state_dir.rename(displaced)
    state_dir.mkdir()
    os.chmod(state_dir, 0o777)

    with pytest.raises(RelayStateBindingError, match="replaced during use"):
        inbox.event_count()

    assert not (state_dir / STATE_BINDING_FILENAME).exists()
    assert not path.exists()
    assert (displaced / STATE_BINDING_FILENAME).exists()
    assert (displaced / "inbox.sqlite3").exists()
    inbox.close()


def test_directory_and_database_store_only_non_secret_account_binding(tmp_path):
    path = tmp_path / "relay" / "inbox.sqlite3"
    binding = RelayStateBinding.for_account(ORIGIN, TOKEN)
    inbox = open_inbox(path)
    inbox.close()

    marker_text = (path.parent / STATE_BINDING_FILENAME).read_text()
    marker = json.loads(marker_text)
    assert marker == binding.record()
    assert TOKEN not in marker_text
    with sqlite3.connect(path) as db:
        row = db.execute(
            """
            SELECT binding_schema,api_origin,
                   token_fingerprint,account_fingerprint
            FROM relay_state_account
            WHERE singleton=1
            """
        ).fetchone()
    assert row == (
        marker["schema"],
        marker["api_origin"],
        marker["token_fingerprint"],
        marker["account_fingerprint"],
    )


def test_token_switch_refuses_before_requeue_or_snapshot_processing(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = open_inbox(path)
    inbox.accept("1", payload("event-1"))
    assert inbox.next_pending()[0] == "event-1"
    inbox.replace_full_snapshot("42", snapshot())
    inbox.close()

    with pytest.raises(RelayStateBindingError, match="different API origin or Agent Token"):
        open_inbox(path, token="different-agent-token")

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT status FROM events WHERE event_id='event-1'"
        ).fetchone()[0] == "processing"
        assert db.execute(
            "SELECT through_sequence FROM full_sync_state WHERE singleton=1"
        ).fetchone()[0] == "42"

    correct = open_inbox(path)
    assert correct.status("event-1") == "pending"
    assert correct.full_sync_state()["through_sequence"] == "42"
    correct.close()


def test_origin_switch_refuses_the_same_state_directory(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    open_inbox(path).close()

    with pytest.raises(RelayStateBindingError, match="different API origin or Agent Token"):
        open_inbox(path, origin="https://api.relayapp.im")

    open_inbox(path).close()


def test_database_switch_is_refused_even_when_directory_binding_matches(tmp_path):
    first = tmp_path / "first" / "inbox.sqlite3"
    second = tmp_path / "second" / "inbox.sqlite3"
    open_inbox(first, token="first-token").close()
    open_inbox(second, token="second-token").close()
    second.unlink()
    shutil.copy2(first, second)

    with pytest.raises(RelayStateBindingError, match="different API origin or Agent Token"):
        open_inbox(second, token="second-token")


def test_live_database_mismatch_blocks_snapshot_reads_and_replacement(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = open_inbox(path)
    original_digest = inbox.replace_full_snapshot("7", snapshot())
    replacement = RelayStateBinding.for_account(ORIGIN, "replacement-token")
    with sqlite3.connect(path) as db:
        db.execute(
            """
            UPDATE relay_state_account
            SET binding_schema=?,api_origin=?,
                token_fingerprint=?,account_fingerprint=?
            WHERE singleton=1
            """,
            (
                replacement.record()["schema"],
                replacement.api_origin,
                replacement.token_fingerprint,
                replacement.account_fingerprint,
            ),
        )

    with pytest.raises(RelayStateBindingError, match="different API origin or Agent Token"):
        inbox.load_full_snapshot()
    with pytest.raises(RelayStateBindingError, match="different API origin or Agent Token"):
        inbox.replace_full_snapshot("invalid-checkpoint", [{"invalid": True}])

    with sqlite3.connect(path) as db:
        assert db.execute(
            """
            SELECT through_sequence,snapshot_sha256
            FROM full_sync_state
            WHERE singleton=1
            """
        ).fetchone() == ("7", original_digest)
    inbox.close()


def test_unbound_existing_database_is_not_adopted_or_requeued(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE events (
              event_id TEXT PRIMARY KEY,
              envelope_json TEXT NOT NULL,
              status TEXT NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO events VALUES('legacy','{}','processing')"
        )

    with pytest.raises(RelayStateBindingError, match="predates account binding"):
        open_inbox(path)

    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT status FROM events WHERE event_id='legacy'"
        ).fetchone()[0] == "processing"
