"""The manifest must stay inside the Hermes plugin contract.

Field names are checked against ``_KNOWN_MANIFEST_FIELDS`` in
hermes_cli/plugins.py, and the env-var dict keys against the documented set in
website/docs/developer-guide/adding-platform-adapters.md ("Surfacing Env Vars
in hermes config").
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

MANIFEST = Path(__file__).resolve().parents[1] / "plugin.yaml"

KNOWN_MANIFEST_FIELDS = {
    "name", "version", "description", "author", "requires_env",
    "provides_tools", "provides_hooks", "kind", "hooks", "label",
    "optional_env", "platforms", "external_dependencies", "pip_dependencies",
    "provides_browser_providers", "provides_web_providers",
    "manifest_version", "api_version", "requires_plugins",
    "python_dependencies", "config_schema", "license", "homepage", "tags",
    "capabilities", "emits", "listens", "hermes", "depends",
}

KNOWN_ENV_KEYS = {"name", "description", "prompt", "url", "password", "category"}


@pytest.fixture(scope="module")
def manifest():
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_every_top_level_key_is_in_the_contract(manifest):
    unknown = set(manifest) - KNOWN_MANIFEST_FIELDS
    assert unknown == set()


def test_declares_a_platform_plugin(manifest):
    assert manifest["kind"] == "platform"
    assert manifest["manifest_version"] == 1
    assert manifest["name"] == "relay-hermes"
    assert manifest["label"] == "Relay"


def test_the_agent_token_is_the_only_hard_requirement(manifest):
    names = [entry["name"] for entry in manifest["requires_env"]]
    assert names == ["RELAY_AGENT_TOKEN"]


def test_env_entries_use_only_documented_keys(manifest):
    entries = manifest["requires_env"] + manifest["optional_env"]
    for entry in entries:
        assert set(entry) <= KNOWN_ENV_KEYS, entry["name"]
        assert entry["description"].strip()
        assert entry["prompt"].strip()


def test_the_token_is_masked_and_points_at_its_docs(manifest):
    token = manifest["requires_env"][0]
    assert token["password"] is True
    assert token["url"] == "https://docs.relayapp.im/getting-started/authentication"


def test_no_secret_is_declared_optional(manifest):
    for entry in manifest["optional_env"]:
        assert entry["password"] is False, entry["name"]


def test_manifest_uses_current_relay_vocabulary(manifest):
    optional = {entry["name"] for entry in manifest["optional_env"]}
    assert {
        "RELAY_ALLOWED_CONTACTS",
        "RELAY_HOME_CHAT",
        "RELAY_HOME_CHAT_NAME",
        "RELAY_GROUP_CHAT_POLICY",
    } <= optional
    assert not any("USER" in name or "CHANNEL" in name for name in optional)


def test_public_repository_metadata_is_canonical(manifest):
    root = MANIFEST.parent
    canonical = "https://github.com/RelayMessenger/Relay-Hermes"
    assert manifest["homepage"] == canonical
    assert canonical in (root / "README.md").read_text(encoding="utf-8")
    assert canonical in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_pip_entrypoint_uses_current_hermes_module_convention():
    metadata = tomllib.loads(
        (MANIFEST.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert metadata["project"]["name"] == "relay-hermes"
    assert metadata["project"]["entry-points"]["hermes_agent.plugins"] == {
        "relay-hermes": "relay_hermes",
    }
    assert metadata["tool"]["setuptools"]["package-data"] == {
        "relay_hermes": ["plugin.yaml"],
    }


@pytest.mark.parametrize("name", ["plugin.yaml", "README.md"])
def test_shipped_copy_carries_no_em_dashes(name):
    text = (MANIFEST.parent / name).read_text(encoding="utf-8")
    assert "\u2014" not in text
