"""Test configuration.

``adapter.py`` binds to the Hermes gateway, so it only imports inside a
Hermes install. The tests cover the transport and state modules, which carry
the delivery semantics and have no host imports at all, so collection skips
the adapter.
"""

collect_ignore = ["adapter.py"]
