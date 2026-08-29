"""Expose the setuptools package layout without importing the Hermes adapter."""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if "hermes_relay_plugin" not in sys.modules:
    package = types.ModuleType("hermes_relay_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules["hermes_relay_plugin"] = package

collect_ignore = ["../adapter.py"]
