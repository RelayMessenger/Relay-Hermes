from __future__ import annotations

import json
import os

import pytest

from hermes_relay_plugin.state import RelayInbox


def payload(event_id: str):
    return {
        "event_id": event_id,
        "event_type": "message.received",
        "data": {"id": "message"},
    }


def test_accept_is_durable_and_deduplicates_event_id(tmp_path):
    path = tmp_path / "relay" / "inbox.sqlite3"
    inbox = RelayInbox(path).open()
    assert inbox.accept("1", payload("event-1")) is True
    assert inbox.accept("2", payload("event-1")) is False
    assert inbox.event_count() == 1
    inbox.close()

    reopened = RelayInbox(path).open()
    assert reopened.status("event-1") == "pending"
    event_id, event = reopened.next_pending()
    assert event_id == "event-1"
    assert event == payload("event-1")
    reopened.complete(event_id)
    assert reopened.status(event_id) == "completed"
    reopened.close()


def test_failed_processing_returns_to_pending(tmp_path):
    inbox = RelayInbox(tmp_path / "inbox.sqlite3").open()
    inbox.accept("9", payload("event-9"))
    event_id, _ = inbox.next_pending()
    assert inbox.status(event_id) == "processing"
    inbox.retry(event_id, "model unavailable")
    assert inbox.status(event_id) == "pending"
    inbox.close()


def test_dispatched_work_is_not_selected_twice_and_recovers_after_restart(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    inbox = RelayInbox(path).open()
    inbox.accept("10", payload("event-10"))
    event_id, _ = inbox.next_pending()
    inbox.mark_dispatched(event_id)
    assert inbox.next_pending() is None
    inbox.close()

    recovered = RelayInbox(path).open()
    assert recovered.status(event_id) == "pending"
    assert recovered.next_pending()[0] == event_id
    recovered.close()


def test_sequence_and_event_id_are_validated(tmp_path):
    inbox = RelayInbox(tmp_path / "inbox.sqlite3").open()
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX file mode")
def test_inbox_is_owner_only(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    RelayInbox(path).open().close()
    assert os.stat(path).st_mode & 0o777 == 0o600
