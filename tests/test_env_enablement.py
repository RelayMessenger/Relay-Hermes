"""Owner-only must survive a second config load in one process."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

ADAPTER = Path(__file__).resolve().parents[1] / "adapter.py"


def test_seed_reads_captured_operator_intent_not_live_env():
    src = ADAPTER.read_text(encoding="utf-8")
    assert "_OPERATOR_ALLOW_ALL = os.getenv(\"RELAY_ALLOW_ALL_USERS\"" in src
    assert '"allow_all_users": _OPERATOR_ALLOW_ALL' in src
    assert '"allow_all_users": os.getenv("RELAY_ALLOW_ALL_USERS"' not in src


def test_second_load_does_not_flip_owner_only(monkeypatch):
    hermes_src = Path.home() / ".hermes/hermes-agent"
    if not hermes_src.is_dir():
        pytest.skip("hermes source is not installed")

    import importlib
    import sys
    import types

    sys.path.insert(0, str(hermes_src))
    pkg_name = "hermes_relay_plugin"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(ADAPTER.parent)]
    sys.modules[pkg_name] = pkg
    monkeypatch.setenv("RELAY_AGENT_TOKEN", "rly_live_test")
    monkeypatch.delenv("RELAY_ALLOW_ALL_USERS", raising=False)

    plugin = importlib.import_module(f"{pkg_name}.adapter")

    first = plugin._env_enablement()
    second = plugin._env_enablement()
    assert first is not None
    assert second is not None
    assert first["allow_all_users"] == ""
    assert second["allow_all_users"] == ""
    assert os.environ.get("RELAY_ALLOW_ALL_USERS") == "true"
