"""The load-time Hermes version gate in relay_hermes/__init__.py.

These run without Hermes: the version lookup and the symbol imports are
replaced, and the message text is what a person would read on the console.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def compat(monkeypatch):
    """Import relay_hermes/__init__.py fresh, as the package ``relay_hermes``."""
    monkeypatch.delitem(sys.modules, "relay_hermes", raising=False)
    spec = importlib.util.spec_from_file_location(
        "relay_hermes", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["relay_hermes"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "plugin_version", lambda: "9.9.9")
    return module


def _install_fake_hermes(monkeypatch, compat, *, drop: str | None = None):
    """Put every module in HERMES_IMPORTS on sys.modules, minus ``drop``."""
    for module_name, symbols in compat.HERMES_IMPORTS:
        fake = types.ModuleType(module_name)
        for symbol in symbols:
            if f"{module_name}.{symbol}" != drop:
                setattr(fake, symbol, object())
        monkeypatch.setitem(sys.modules, module_name, fake)


def test_supported_range_matches_package_policy():
    """Keep the minimum CI proof and packaging policy aligned."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    text = (ROOT / "__init__.py").read_text(encoding="utf-8")
    assert 'HERMES_MIN_VERSION = "0.19.0"' in text
    assert "HERMES_PUBLISHED_VERSION: 0.19.0" in ci
    assert 'HERMES_MAX_TESTED_VERSION = "0.21.3"' in text
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"hermes-agent>=0.19.0,<0.22"' in pyproject


def test_missing_hermes_fails_loudly(compat, monkeypatch, capsys):
    monkeypatch.setattr(compat, "hermes_version", lambda: None)
    with pytest.raises(compat.HermesVersionError) as raised:
        compat.check_hermes()
    message = str(raised.value)
    assert message.startswith("relay-hermes 9.9.9 cannot load: hermes-agent is not installed")
    assert "Supported: hermes-agent >= 0.19.0; tested through 0.21.3 (newer minors warn and run)." in message
    assert capsys.readouterr().err.strip() == message


def test_older_hermes_fails_loudly(compat, monkeypatch, capsys):
    monkeypatch.setattr(compat, "hermes_version", lambda: "0.18.9")
    with pytest.raises(compat.HermesVersionError) as raised:
        compat.check_hermes()
    message = str(raised.value)
    assert "relay-hermes 9.9.9 cannot load: found hermes-agent 0.18.9" in message
    assert "older than 0.19.0" in message
    assert "Supported: hermes-agent >= 0.19.0; tested through 0.21.3 (newer minors warn and run)." in message
    assert capsys.readouterr().err.strip() == message


def test_missing_symbol_is_named(compat, monkeypatch, capsys):
    monkeypatch.setattr(compat, "hermes_version", lambda: "0.21.1")
    _install_fake_hermes(monkeypatch, compat, drop="gateway.platforms.helpers.strip_markdown")
    with pytest.raises(compat.HermesVersionError) as raised:
        compat.check_hermes()
    message = str(raised.value)
    assert "hermes-agent 0.21.1 is missing gateway.platforms.helpers.strip_markdown" in message
    assert "Supported: hermes-agent >= 0.19.0; tested through 0.21.3 (newer minors warn and run)." in message
    assert capsys.readouterr().err.strip() == message


def test_supported_hermes_loads_silently(compat, monkeypatch, capsys):
    monkeypatch.setattr(compat, "hermes_version", lambda: "0.19.0")
    _install_fake_hermes(monkeypatch, compat)
    assert compat.check_hermes() == "0.19.0"
    assert capsys.readouterr().err == ""


def test_0219_accepted_silently(compat, monkeypatch, capsys, caplog):
    monkeypatch.setattr(compat, "hermes_version", lambda: "0.21.9")
    _install_fake_hermes(monkeypatch, compat)
    assert compat.check_hermes() == "0.21.9"
    assert capsys.readouterr().err == ""
    assert caplog.records == []


@pytest.mark.parametrize("found", ["0.22.0", "1.0.0"])
def test_newer_minor_warns_and_continues(compat, monkeypatch, capsys, caplog, found):
    monkeypatch.setattr(compat, "hermes_version", lambda: found)
    _install_fake_hermes(monkeypatch, compat)
    assert compat.check_hermes() == found
    assert capsys.readouterr().err == ""
    assert [(record.levelname, record.getMessage()) for record in caplog.records] == [
        ("WARNING", f"Relay plugin tested through Hermes 0.21.3; you have {found}. It will try to run.")
    ]


def test_register_checks_before_importing_the_adapter(compat, monkeypatch):
    # A None entry makes ``import relay_hermes.adapter`` raise a plain
    # ImportError, so the gate's own error is the only way to pass.
    monkeypatch.setitem(sys.modules, "relay_hermes.adapter", None)
    monkeypatch.setattr(compat, "hermes_version", lambda: None)
    with pytest.raises(ImportError) as raised:
        compat.register(None)
    assert raised.type is compat.HermesVersionError


def test_parse_version():
    assert compat_parse("0.19.0") == (0, 19, 0)
    assert compat_parse("0.21.1.dev3") == (0, 21, 1)
    assert compat_parse("0.19.0") < compat_parse("0.21.1")


def compat_parse(text):
    spec = importlib.util.spec_from_file_location("_relay_hermes_parse", ROOT / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_version(text)
