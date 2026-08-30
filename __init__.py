"""Relay plugin for Hermes Agent.

Deliberately import-light. Platform plugins are deferred: the adapter module
and its imports load only when the platform registry is first asked for the
platform, which the adding-platform-adapters guide states as the contract
("Keep the package __init__.py import-light and pull the adapter in from
inside register()"). Importing the adapter here would drag the whole Hermes
gateway into every process that merely discovers plugins.
"""


def register(ctx):
    """Plugin entry point. Imports the adapter on first use, not at discovery."""
    from .adapter import register as _register

    return _register(ctx)


__all__ = ["register"]
