"""The manifest must stay inside the Hermes plugin contract.

Field names are checked against ``_KNOWN_MANIFEST_FIELDS`` in
hermes_cli/plugins.py, and the env-var dict keys against the documented set in
website/docs/developer-guide/adding-platform-adapters.md ("Surfacing Env Vars
in hermes config").
"""

from __future__ import annotations

import re
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
    assert "RelayMessenger/Relay-Hermes" in (
        root / ".github" / "workflows" / "publish-rc.yml"
    ).read_text(encoding="utf-8")
    public_files = [
        root / "README.md",
        root / "plugin.yaml",
        root / "pyproject.toml",
        *sorted((root / ".github" / "workflows").glob("*.yml")),
    ]
    for path in public_files:
        assert "hermes-relay-plugin" not in path.read_text(encoding="utf-8")


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


def test_staging_package_and_manifest_versions_match(manifest):
    metadata = tomllib.loads(
        (MANIFEST.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert metadata["project"]["version"] == "1.0.0rc1"
    assert manifest["version"] == "1.0.0-rc.1"


def test_hosted_workflows_pin_every_external_action_to_a_sha():
    workflows = MANIFEST.parent / ".github" / "workflows"
    uses_pattern = re.compile(r"^\s*uses:\s*([^#\s]+)", re.MULTILINE)
    sha_pattern = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for path in sorted(workflows.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        uses = uses_pattern.findall(text)
        assert uses, path.name
        external = [value for value in uses if not value.startswith("./")]
        assert all(
            sha_pattern.fullmatch(value)
            for value in external
        ), path.name
        parsed = yaml.safe_load(text)
        for job in parsed["jobs"].values():
            for step in job.get("steps", []):
                assert "${{" not in step.get("run", ""), path.name


def test_rc_publish_is_manual_exact_sha_staging_only():
    path = MANIFEST.parent / ".github" / "workflows" / "publish-rc.yml"
    text = path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    jobs = parsed["jobs"]
    assert "workflow_dispatch:" in text
    assert "github.ref == 'refs/heads/staging'" in text
    assert "github.repository == 'RelayMessenger/Relay-Hermes'" in text
    assert 'environment: pypi-rc' in text
    assert 'test "$EXPECTED_SHA" = "$EVENT_SHA"' in text
    assert "id-token: write" in text
    assert "attest-build-provenance@" in text
    assert "password:" not in text
    assert jobs["validate"]["needs"] == "preflight"
    assert jobs["validate"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["contract"]["needs"] == "validate"
    assert jobs["build"]["needs"] == "contract"
    assert jobs["publish"]["needs"] == "build"
    assert "RELAY_CONTRACT_READ_TOKEN" in text
    assert "scripts/check-openapi.py" in text
    assert text.count("environment: release-candidate") == 2
    assert 'case "$EXPECTED_SHA" in' in text
    assert 'test "${#EXPECTED_SHA}" -eq 40' in text


def test_reusable_ci_covers_full_release_compatibility():
    path = MANIFEST.parent / ".github" / "workflows" / "ci.yml"
    text = path.read_text(encoding="utf-8")
    assert "workflow_call:" in text
    assert 'python-version: ["3.11", "3.12", "3.13"]' in text
    assert "Run the full Hermes integration suite" in text
    assert "Run Hermes Plugin Doctor" in text
    assert "Test the staging helper fail-closed guards" in text
    assert "Clean-install wheel and sdist" in text
    assert "Run Hermes against the clean wheel" in text


@pytest.mark.parametrize("name", ["plugin.yaml", "README.md"])
def test_shipped_copy_carries_no_em_dashes(name):
    text = (MANIFEST.parent / name).read_text(encoding="utf-8")
    assert "\u2014" not in text
