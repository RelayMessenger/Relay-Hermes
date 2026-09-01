from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = Path(
    os.environ.get("HERMES_AGENT_SRC", "").strip()
    or ROOT.parent / "_sources/hermes-agent"
)

pytestmark = pytest.mark.skipif(
    not HERMES_SOURCE.is_dir(),
    reason="Hermes source is required for plugin runtime tests",
)


def test_current_hermes_directory_plugin_harness(tmp_path):
    """Load the repository through Hermes's real user-plugin discovery path."""

    home = tmp_path / "hermes-home"
    installed = home / "plugins" / "Relay-Hermes"
    shutil.copytree(
        ROOT,
        installed,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            "build",
            "dist",
            "*.egg-info",
        ),
    )
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - relay-hermes\n",
        encoding="utf-8",
    )

    script = """
import json
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import PluginManager

manager = PluginManager()
manager.discover_and_load()
loaded = manager.list_plugins()
plugin = next(item for item in loaded if item["name"] == "relay-hermes")
entry = platform_registry.get("relayapp")
assert plugin["enabled"] is True, plugin
assert plugin["error"] is None, plugin
assert entry is not None
assert entry.required_env == ["RELAY_AGENT_TOKEN"]
adapter = entry.adapter_factory(PlatformConfig(extra={
    "token": "relay-test-token",
    "state_dir": __import__("os").environ["RELAY_STATE_DIR"],
}))
assert type(adapter).__name__ == "RelayAdapter"
assert adapter.platform.value == "relayapp"
print(json.dumps({
    "plugin": plugin["name"],
    "platform": entry.name,
    "adapter": type(adapter).__name__,
}))
"""
    env = dict(os.environ)
    env.update({
        "HERMES_HOME": str(home),
        "HERMES_AGENT_SRC": str(HERMES_SOURCE),
        "PYTHONPATH": str(HERMES_SOURCE),
        "RELAY_AGENT_TOKEN": "relay-test-token",
        "RELAY_STATE_DIR": str(home / "relay-state"),
    })
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=HERMES_SOURCE,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "plugin": "relay-hermes",
        "platform": "relayapp",
        "adapter": "RelayAdapter",
    }


def test_current_hermes_pip_entrypoint_harness(tmp_path):
    """Load the editable/wheel metadata through Hermes's entry-point path."""

    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - relay-hermes\n",
        encoding="utf-8",
    )
    script = """
import json
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import PluginManager

manager = PluginManager()
manager.discover_and_load()
plugin = next(
    item for item in manager.list_plugins()
    if item["name"] == "relay-hermes" and item["source"] == "entrypoint"
)
entry = platform_registry.get("relayapp")
assert plugin["enabled"] is True, plugin
assert plugin["error"] is None, plugin
assert entry is not None
print(json.dumps({
    "plugin": plugin["name"],
    "source": plugin["source"],
    "platform": entry.name,
}))
"""
    env = dict(os.environ)
    env.update({
        "HERMES_HOME": str(home),
        "PYTHONPATH": str(HERMES_SOURCE),
        "RELAY_AGENT_TOKEN": "relay-test-token",
        "RELAY_STATE_DIR": str(home / "relay-state"),
    })
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=HERMES_SOURCE,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "plugin": "relay-hermes",
        "source": "entrypoint",
        "platform": "relayapp",
    }
