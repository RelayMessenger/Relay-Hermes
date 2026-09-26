"""``hermes plugins install`` scans the cloned plugin before it installs it.

Hermes blocks a community plugin whose scan says "caution" unless the person
answers a prompt or passes ``--force`` (hermes_cli/plugins_cmd.py
``_scan_plugin_tree``; tools/plugin_guard.py ``should_allow_plugin_install``).
Relay-Hermes 4a2eab5 scanned "caution, 35 findings" on Hermes main and could
not be installed from a script. Any HIGH or CRITICAL finding is caution or
worse (tools/skills_guard.py ``_determine_verdict``); medium and low are
informational. This runs the guard of the Hermes named by
``HERMES_GUARD_SRC`` over this repository, as the installer would.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD_SOURCE = os.environ.get("HERMES_GUARD_SRC", "").strip()

pytestmark = pytest.mark.skipif(
    not GUARD_SOURCE, reason="HERMES_GUARD_SRC names the Hermes whose install scan to run"
)


def test_hermes_install_scan_allows_the_plugin_without_force():
    sys.path.insert(0, GUARD_SOURCE)
    try:
        from tools.plugin_guard import scan_plugin, should_allow_plugin_install
    finally:
        sys.path.remove(GUARD_SOURCE)

    result = scan_plugin(ROOT, source="RelayMessenger/Relay-Hermes")
    blocking = [
        f"{finding.severity} {finding.file}:{finding.line} {finding.match[:80]}"
        for finding in result.findings
        if finding.severity in {"high", "critical"}
    ]
    assert blocking == []
    assert result.verdict == "safe"
    assert should_allow_plugin_install(result) == (True, "Allowed (clean scan)")
