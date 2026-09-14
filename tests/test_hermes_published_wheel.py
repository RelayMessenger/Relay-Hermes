"""The plugin loads against the Hermes wheel PyPI ships, not only the git pin.

``HERMES_PUBLISHED_PYTHON`` names the interpreter of a venv holding
``hermes-agent==<HERMES_PUBLISHED_VERSION>`` from PyPI and this plugin
(CI's "Validate against the published Hermes wheel" job builds it). The
suite in tests/test_hermes_runtime.py runs against the git pin; this one is
the receipt for the 2026-09-09 defect, where 1.0.0rc3 imported a module no
published wheel contains and Hermes said only "No messaging platforms
enabled".
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

PYTHON = os.environ.get("HERMES_PUBLISHED_PYTHON", "").strip()
EXPECTED_VERSION = os.environ.get("HERMES_PUBLISHED_VERSION", "0.19.0").strip()

pytestmark = pytest.mark.skipif(
    not PYTHON, reason="HERMES_PUBLISHED_PYTHON names a venv with the PyPI Hermes wheel"
)

PROBE = """
import json
from importlib.metadata import version
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import PluginManager
import relay_hermes

manager = PluginManager()
manager.discover_and_load()
plugin = next(
    item for item in manager.list_plugins()
    if item["name"] == "relay-hermes" and item["source"] == "entrypoint"
)
entry = platform_registry.get("relayapp")
adapter = entry.adapter_factory(PlatformConfig(extra={
    "token": "relay-test-token",
    "state_dir": __import__("os").environ["RELAY_STATE_DIR"],
})) if entry else None
print(json.dumps({
    "hermes": version("hermes-agent"),
    "checked": relay_hermes.hermes_version(),
    "enabled": plugin["enabled"],
    "error": plugin["error"],
    "platform": entry.name if entry else None,
    "adapter": type(adapter).__name__ if adapter else None,
}))
"""


def test_plugin_loads_on_the_published_wheel(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - relay-hermes\n", encoding="utf-8"
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update({
        "HERMES_HOME": str(home),
        "RELAY_AGENT_TOKEN": "relay-test-token",
        "RELAY_STATE_DIR": str(home / "relay-state"),
    })
    completed = subprocess.run(
        [PYTHON, "-c", PROBE],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "hermes": EXPECTED_VERSION,
        "checked": EXPECTED_VERSION,
        "enabled": True,
        "error": None,
        "platform": "relayapp",
        "adapter": "RelayAdapter",
    }
    assert "cannot load" not in completed.stderr
