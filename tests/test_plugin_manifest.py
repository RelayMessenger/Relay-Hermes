"""The manifest must stay inside the Hermes plugin contract.

Field names are checked against ``_KNOWN_MANIFEST_FIELDS`` in
hermes_cli/plugins.py, and the env-var dict keys against the documented set in
website/docs/developer-guide/adding-platform-adapters.md ("Surfacing Env Vars
in hermes config").
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

MANIFEST = Path(__file__).resolve().parents[1] / "plugin.yaml"
OPENAPI_COMMIT = "1a2245dd775f781b57e0d1f6f3146ebd384c90c3"
WORKFLOWS = MANIFEST.parent / ".github" / "workflows"
PUBLISH_ACTION = MANIFEST.parent / ".github" / "actions" / "publish-pypi" / "action.yml"

_spec = importlib.util.spec_from_file_location(
    "release_version", MANIFEST.parent / "scripts" / "release_version.py"
)
release_version = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release_version
_spec.loader.exec_module(release_version)
OPENAPI_SHA256 = (
    "5458497fe8db4ee7dfe6bef67f2803137575d3ea4d835748290a5c9f8d906791"
)
OPENAPI_RELATIVE_PATH = (
    f"contracts/relay-server/{OPENAPI_COMMIT}/openapi.yaml"
)

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
    assert "RELAY_OPERATOR_CONTACTS" not in optional
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
    # The staging lane rewrites both files on every publish, so no literal
    # version is pinned here: pyproject.toml carries PEP 440 (1.0.0rc3,
    # 1.0.0rc3.dev0, 1.0.0.dev0, 1.0.0) and plugin.yaml carries its manifest
    # form (1.0.0-rc.3, 1.0.0-rc.3.dev.0, 1.0.0-dev.0, 1.0.0), one to one.
    metadata = tomllib.loads(
        (MANIFEST.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    version = metadata["project"]["version"]
    assert release_version.parse_version(version)
    assert manifest["version"] == release_version.manifest_form(version)
    assert release_version.pep440_form(manifest["version"]) == version
    assert metadata["build-system"]["requires"] == ["setuptools==84.0.0"]


def test_locked_openapi_snapshot_is_exact_and_provenanced():
    root = MANIFEST.parent
    snapshot = root / OPENAPI_RELATIVE_PATH
    raw = snapshot.read_bytes()
    metadata = (
        root / "contracts" / "relay-server" / "README.md"
    ).read_text(encoding="utf-8")
    harness = (root / "scripts" / "check-openapi.py").read_text(
        encoding="utf-8"
    )
    assert len(raw) == 156157
    assert hashlib.sha256(raw).hexdigest() == OPENAPI_SHA256
    assert OPENAPI_COMMIT in metadata
    assert OPENAPI_SHA256 in metadata
    assert "contracts/developer/openapi.yaml" in metadata
    local_snapshot = OPENAPI_RELATIVE_PATH.removeprefix(
        "contracts/relay-server/"
    )
    assert local_snapshot in metadata
    assert OPENAPI_COMMIT in harness
    assert OPENAPI_SHA256 in harness


def test_hosted_workflows_pin_every_external_action_to_a_sha():
    uses_pattern = re.compile(r"^\s*uses:\s*([^#\s]+)", re.MULTILINE)
    sha_pattern = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for path in [*sorted(WORKFLOWS.glob("*.yml")), PUBLISH_ACTION]:
        text = path.read_text(encoding="utf-8")
        uses = uses_pattern.findall(text)
        assert uses, path.name
        external = [value for value in uses if not value.startswith("./")]
        assert all(
            sha_pattern.fullmatch(value)
            for value in external
        ), path.name
        parsed = yaml.safe_load(text)
        jobs = parsed["jobs"].values() if "jobs" in parsed else [parsed["runs"]]
        for job in jobs:
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
    # PyPI has no trusted publisher for this repository, so the RC uploads
    # with a project-scoped API token. The token must reach the job as a
    # secret reference and never as a literal in the tracked workflow.
    assert "password: ${{ secrets.PYPI_API_TOKEN }}" in text
    assert not re.search(r"pypi-[A-Za-z0-9_-]{16,}", text)
    # PEP 740 attestations only work through Trusted Publishing. With a
    # token the upload step ignores the input, so it stays off on purpose.
    assert "attestations: false" in text
    assert jobs["validate"]["needs"] == "preflight"
    assert jobs["validate"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["contract"]["needs"] == "validate"
    assert jobs["provenance"]["needs"] == "contract"
    assert jobs["publish"]["needs"] == "provenance"
    assert "RELAY_CONTRACT_READ_TOKEN" not in text
    assert "RelayMessenger/Relay-Server" not in text
    assert "_relay-server" not in text
    assert text.count(OPENAPI_RELATIVE_PATH) == 1
    assert "scripts/check-openapi.py" in text
    assert text.count("environment: release-candidate") == 2
    assert 'case "$EXPECTED_SHA" in' in text
    assert 'test "${#EXPECTED_SHA}" -eq 40' in text
    assert "python -m build" not in text
    assert text.count("relay-hermes-validated-rc") == 1
    assert text.count("sha256sum --check provenance/SHA256SUMS") == 2


def test_reusable_ci_covers_full_release_compatibility():
    path = MANIFEST.parent / ".github" / "workflows" / "ci.yml"
    text = path.read_text(encoding="utf-8")
    assert "workflow_call:" in text
    assert 'python-version: ["3.11", "3.12", "3.13"]' in text
    assert text.count("python -m build --outdir release/dist") == 1
    assert "Retain the distributions before validation" in text
    assert "Download the one CI-built distribution pair" in text
    assert "Run the full Hermes integration suite" in text
    assert "Run Hermes Plugin Doctor on the exact sdist" in text
    assert "Test the staging helper fail-closed guards" in text
    assert "Clean-install the exact wheel and sdist" in text
    assert "Run Hermes against the exact clean wheel" in text
    assert "Validate the exact checked-in Relay contract snapshot" in text
    assert text.count(OPENAPI_RELATIVE_PATH) == 1
    assert 'python scripts/check-openapi.py "$RELAY_OPENAPI_SNAPSHOT"' in text
    assert "setuptools==84.0.0" in text
    assert "build==1.6.0" in text


def test_ci_reads_the_version_from_the_tree_and_rehearses_both_lanes():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    # The staging lane rewrites the version on every publish; a literal here
    # would go red on the first bump commit.
    assert "EXPECTED_PACKAGE_VERSION:" not in text
    assert text.count('python scripts/release_version.py show | tee -a "$GITHUB_ENV"') == 2
    assert 'os.environ["EXPECTED_MANIFEST_VERSION"]' in text
    assert set(jobs) == {"build", "test", "staging-bump-dry-run", "release-dry-run"}
    assert "python scripts/release_version.py staging --dry-run" in text
    assert "python scripts/release_version.py release --dry-run" in text
    assert "grep -q '^release plan only: skip$' plan-b.txt" in text
    assert text.count('run: test -z "$(git status --porcelain)"') == 2


def test_release_lanes_share_one_publish_step_switched_by_a_variable():
    action = PUBLISH_ACTION.read_text(encoding="utf-8")
    steps = yaml.safe_load(action)["runs"]["steps"]
    publishers = [step for step in steps if "pypa/gh-action-pypi-publish" in step.get("uses", "")]
    assert len(publishers) == 2
    token, oidc = publishers
    assert token["if"] == "inputs.trusted-publishing == ''"
    assert token["with"]["password"] == "${{ inputs.token }}"
    assert token["with"]["attestations"] is False
    assert oidc["if"] == "inputs.trusted-publishing == 'true'"
    assert "password" not in oidc["with"]
    assert oidc["with"]["attestations"] is True
    assert publishers[0]["uses"] == publishers[1]["uses"]
    assert not re.search(r"pypi-[A-Za-z0-9_-]{16,}", action)

    lanes = {
        "publish-staging.yml": ("staging", "pypi-staging", "publish"),
        "release.yml": ("main", "pypi-release", "release"),
    }
    for name, (branch, environment, job_name) in lanes.items():
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
        assert parsed[True]["push"]["branches"] == [branch], name
        job = parsed["jobs"][job_name]
        assert job["environment"] == environment, name
        assert job["permissions"]["id-token"] == "write", name
        publish = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/publish-pypi")
        assert publish["with"]["trusted-publishing"] == "${{ vars.PYPI_TRUSTED_PUBLISHING }}", name
        assert publish["with"]["token"] == "${{ secrets.PYPI_API_TOKEN }}", name
        assert "github.repository == 'RelayMessenger/Relay-Hermes'" in text, name
        assert "python scripts/release_version.py verify-published" in text, name
        assert "password:" not in text, name
        assert not re.search(r"pypi-[A-Za-z0-9_-]{16,}", text), name

    staging = (WORKFLOWS / "publish-staging.yml").read_text(encoding="utf-8")
    assert "release: version the staging package automatically" in staging
    assert "python scripts/release_version.py staging --write" in staging
    assert "git add pyproject.toml plugin.yaml" in staging
    assert yaml.safe_load(staging)["jobs"]["validate"]["uses"] == "./.github/workflows/ci.yml"
    assert staging.count("relay-hermes-validated-rc") == 1

    release = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
    assert "python scripts/release_version.py release --write" in release
    assert "git merge-base --is-ancestor HEAD origin/staging" in release
    assert "A manual run of this workflow only ever dry-runs." in release
    assert "--forbid \"$STAGING_VERSION\"" in release
    assert 'ref="refs/tags/${RELEASE_TAG}"' in release or "refs/tags/${RELEASE_TAG}" in release


@pytest.mark.parametrize("name", ["plugin.yaml", "README.md"])
def test_shipped_copy_carries_no_em_dashes(name):
    text = (MANIFEST.parent / name).read_text(encoding="utf-8")
    assert "\u2014" not in text
