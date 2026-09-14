"""Relay plugin for Hermes Agent.

Deliberately import-light. Platform plugins are deferred: the adapter module
and its imports load only when the platform registry is first asked for the
platform, which the adding-platform-adapters guide states as the contract
("Keep the package __init__.py import-light and pull the adapter in from
inside register()"). Importing the adapter here would drag the whole Hermes
gateway into every process that merely discovers plugins.

Before the adapter is imported, :func:`check_hermes` proves the Hermes this
process runs is one the adapter was written against. Without it a missing
Hermes symbol surfaces as a ``ModuleNotFoundError`` that Hermes's plugin
loader swallows into ``~/.hermes/logs/errors.log`` while the console says
only ``No messaging platforms enabled`` (relay-hermes 1.0.0rc3 on the PyPI
hermes-agent 0.19.0 wheel, measured 2026-09-09).
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from pathlib import Path

PLUGIN_NAME = "relay-hermes"
HERMES_DISTRIBUTION = "hermes-agent"

# The oldest Hermes the adapter is proven against: 0.19.0 (2026-07-20) is the
# newest wheel PyPI ships, and the whole test suite passes on it. Older
# wheels were never run and are refused.
HERMES_MIN_VERSION = "0.19.0"
# The newest Hermes the adapter is proven against: CI's git pin, commit
# b2aa855 (2026-09-08), whose pyproject says 0.21.1. Newer versions
# must be tested before the supported range is extended.
HERMES_MAX_TESTED_VERSION = "0.21.1"
SUPPORTED_HERMES = (
    f"{HERMES_DISTRIBUTION} {HERMES_MIN_VERSION} through {HERMES_MAX_TESTED_VERSION}"
)

# Every Hermes symbol adapter.py and state.py import, checked as a set so a
# missing one is named here, with both versions, instead of by a bare
# ModuleNotFoundError from the middle of the adapter.
HERMES_IMPORTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("agent.secret_scope", ("UnscopedSecretError", "get_secret")),
    ("gateway.config", ("Platform", "PlatformConfig")),
    (
        "gateway.platforms.base",
        (
            "BasePlatformAdapter",
            "MessageEvent",
            "MessageType",
            "ProcessingOutcome",
            "SendResult",
            "cache_image_from_bytes",
        ),
    ),
    ("gateway.platforms.helpers", ("strip_markdown",)),
    ("gateway.session", ("build_session_key",)),
    ("hermes_cli.config", ("get_hermes_home",)),
)

logger = logging.getLogger(__name__)


class HermesVersionError(ImportError):
    """The Hermes in this process is missing or is not one relay-hermes supports."""


def plugin_version() -> str:
    """This plugin's version: the installed distribution, else plugin.yaml."""
    try:
        return _distribution_version(PLUGIN_NAME)
    except PackageNotFoundError:
        pass
    manifest = Path(__file__).with_name("plugin.yaml")
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r"^version:\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def hermes_version() -> str | None:
    """The Hermes version in this process, or None when Hermes is absent.

    The distribution metadata is authoritative (pip, uv and an editable git
    checkout all write it). ``hermes_cli.__version__`` covers a bare source
    tree on ``PYTHONPATH``, which is how the plugin's own tests run.
    """
    try:
        return _distribution_version(HERMES_DISTRIBUTION)
    except PackageNotFoundError:
        pass
    try:
        module = importlib.import_module("hermes_cli")
    except ImportError:
        return None
    found = getattr(module, "__version__", None)
    return str(found) if found else None


def parse_version(text: str) -> tuple[int, ...]:
    """Leading dotted integers of a version string; ``"0.19.0"`` -> ``(0, 19, 0)``."""
    parts: list[int] = []
    for piece in text.split("."):
        match = re.match(r"\d+", piece)
        if match is None:
            break
        parts.append(int(match.group(0)))
    return tuple(parts)


def _report(message: str, *, error: bool) -> None:
    # Hermes loads plugins before it attaches its console log handler, so a
    # record logged here reaches only ~/.hermes/logs/errors.log (measured on
    # 0.19.0: the loader's own "Failed to load plugin" warning never printed).
    # The console line is written directly; the log record is for the file.
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    (logger.error if error else logger.warning)(message)


def check_hermes() -> str:
    """Return the Hermes version found, or raise :class:`HermesVersionError`.

    The error message names this plugin and its version, the Hermes version
    found (or its absence), and the versions supported, so a person reading
    the console or ``hermes plugins list`` knows what to install.
    """
    prefix = f"{PLUGIN_NAME} {plugin_version()} cannot load"
    found = hermes_version()
    if found is None:
        message = (
            f"{prefix}: {HERMES_DISTRIBUTION} is not installed in this Python "
            f"({sys.executable}). Supported: {SUPPORTED_HERMES}."
        )
        _report(message, error=True)
        raise HermesVersionError(message)
    if parse_version(found) < parse_version(HERMES_MIN_VERSION):
        message = (
            f"{prefix}: found {HERMES_DISTRIBUTION} {found}, which is older "
            f"than {HERMES_MIN_VERSION}. Supported: {SUPPORTED_HERMES}."
        )
        _report(message, error=True)
        raise HermesVersionError(message)
    if parse_version(found) > parse_version(HERMES_MAX_TESTED_VERSION):
        message = (
            f"{prefix}: found {HERMES_DISTRIBUTION} {found}, which is newer "
            f"than {HERMES_MAX_TESTED_VERSION}. Supported: {SUPPORTED_HERMES}."
        )
        _report(message, error=True)
        raise HermesVersionError(message)
    missing: list[str] = []
    for module_name, symbols in HERMES_IMPORTS:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
            continue
        missing.extend(
            f"{module_name}.{symbol}"
            for symbol in symbols
            if not hasattr(module, symbol)
        )
    if missing:
        message = (
            f"{prefix}: {HERMES_DISTRIBUTION} {found} is missing "
            f"{', '.join(missing)}, which this plugin needs. "
            f"Supported: {SUPPORTED_HERMES}."
        )
        _report(message, error=True)
        raise HermesVersionError(message)
    return found


def register(ctx):
    """Plugin entry point. Proves the Hermes version, then imports the adapter."""
    check_hermes()
    from .adapter import register as _register

    return _register(ctx)


__all__ = [
    "HERMES_MAX_TESTED_VERSION",
    "HERMES_MIN_VERSION",
    "HermesVersionError",
    "SUPPORTED_HERMES",
    "check_hermes",
    "hermes_version",
    "register",
]
