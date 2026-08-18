"""State persistence tests: atomic writes, monotonic cursor, corrupt files."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from state import RelayState  # noqa: E402


def make_state(tmp_path: Path, **kwargs) -> RelayState:
    return RelayState(tmp_path / "relay" / "state.json", **kwargs)


def test_missing_file_starts_at_zero(tmp_path):
    state = make_state(tmp_path).load()
    assert state.cursor == 0
    assert state.seen_event_ids() == []


def test_advance_persists_cursor_and_window(tmp_path):
    state = make_state(tmp_path).load()
    assert state.advance(7, ["evt_1", "evt_2"]) is True

    reloaded = make_state(tmp_path).load()
    assert reloaded.cursor == 7
    assert reloaded.seen_event_ids() == ["evt_1", "evt_2"]


def test_cursor_is_monotonic(tmp_path):
    state = make_state(tmp_path).load()
    state.advance(10, [])
    assert state.advance(4, []) is False
    assert state.advance(10, []) is False
    assert state.cursor == 10

    reloaded = make_state(tmp_path).load()
    assert reloaded.cursor == 10


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    state = make_state(tmp_path).load()
    state.advance(3, ["evt_1"])
    directory = (tmp_path / "relay")
    names = sorted(p.name for p in directory.iterdir())
    assert names == ["state.json"]
    payload = json.loads((directory / "state.json").read_text())
    assert payload == {"cursor": 3, "seen_event_ids": ["evt_1"]}


def test_state_file_is_owner_readable_only(tmp_path):
    state = make_state(tmp_path).load()
    state.advance(1, [])
    mode = os.stat(tmp_path / "relay" / "state.json").st_mode & 0o777
    assert mode == 0o600


def test_dedupe_window_is_capped(tmp_path):
    state = make_state(tmp_path, dedupe_capacity=2).load()
    state.advance(1, ["a", "b", "c"])
    assert state.seen_event_ids() == ["b", "c"]


def test_corrupt_file_is_ignored_rather_than_fatal(tmp_path):
    path = tmp_path / "relay" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json at all")
    state = RelayState(path).load()
    assert state.cursor == 0


def test_negative_cursor_on_disk_is_ignored(tmp_path):
    path = tmp_path / "relay" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cursor": -5, "seen_event_ids": []}))
    assert RelayState(path).load().cursor == 0


def test_boolean_cursor_is_not_mistaken_for_an_integer(tmp_path):
    path = tmp_path / "relay" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"cursor": True, "seen_event_ids": []}))
    assert RelayState(path).load().cursor == 0


def test_use_before_load_is_refused(tmp_path):
    state = make_state(tmp_path)
    with pytest.raises(RuntimeError):
        _ = state.cursor
    with pytest.raises(RuntimeError):
        state.advance(1, [])
