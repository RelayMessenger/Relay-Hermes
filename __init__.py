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

# The oldest Hermes the adapter is proven against. Older versions are refused.
HERMES_MIN_VERSION = "0.19.0"
# Patch releases in the tested minor load silently; newer minors warn and run.
HERMES_MAX_TESTED_VERSION = "0.21.5"
SUPPORTED_HERMES = (
    f"{HERMES_DISTRIBUTION} >= {HERMES_MIN_VERSION}; "
    f"tested through {HERMES_MAX_TESTED_VERSION} (newer minors warn and run)"
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


# Hermes main commits a placeholder package version: "0.0.0" in pyproject.toml
# since its commit 9cc319f9a1 (2026-09-21, "main carries 0.0.0 ... a build
# stamps the real one"). That value is not a release and is never compared.
_PLACEHOLDER_VERSIONS = {"0.0.0", "unknown", ""}


def hermes_version() -> str | None:
    """The version Hermes reports for itself, or None when Hermes is absent.

    Hermes's own answer comes first: ``hermes_cli.version_info
    .get_version_info().base_version``, which reads the install stamp a
    build writes, else the checkout's release tags. That is the value Hermes
    gates a plugin's ``requires_hermes`` on (``hermes_cli.plugins_manifest
    .running_hermes_version``). Older Hermes wheels have no ``version_info``;
    their distribution metadata is the real release. A placeholder from
    either source is reported as ``"unknown"``, never as a release.
    """
    try:
        from hermes_cli.version_info import get_version_info
    except ImportError:
        pass
    else:
        try:
            found = str(get_version_info().base_version or "").strip()
        except Exception:  # noqa: BLE001 - a broken stamp must not stop the load
            found = ""
        if found not in _PLACEHOLDER_VERSIONS:
            return found
    try:
        found = _distribution_version(HERMES_DISTRIBUTION)
    except PackageNotFoundError:
        found = None
    if found is not None:
        return found if found not in _PLACEHOLDER_VERSIONS else "unknown"
    try:
        module = importlib.import_module("hermes_cli")
    except ImportError:
        return None
    found = str(getattr(module, "__version__", "") or "")
    return found if found not in _PLACEHOLDER_VERSIONS else "unknown"


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
    # A version Hermes cannot name is not compared: the symbol check below
    # decides whether the adapter can run on it.
    known = found != "unknown" and bool(parse_version(found))
    if known and parse_version(found) < parse_version(HERMES_MIN_VERSION):
        message = (
            f"{prefix}: found {HERMES_DISTRIBUTION} {found}, which is older "
            f"than {HERMES_MIN_VERSION}. Supported: {SUPPORTED_HERMES}."
        )
        _report(message, error=True)
        raise HermesVersionError(message)
    if known and parse_version(found)[:2] > parse_version(HERMES_MAX_TESTED_VERSION)[:2]:
        logger.warning(
            "Relay plugin tested through Hermes %s; you have %s. It will try to run.",
            HERMES_MAX_TESTED_VERSION,
            found,
        )
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
