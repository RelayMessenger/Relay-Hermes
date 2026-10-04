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
OPENAPI_COMMIT = "78e958bd35f5e7f33c1ce9b77ac11be1dac3afc8"
WORKFLOWS = MANIFEST.parent / ".github" / "workflows"
CHECK_ACTION = MANIFEST.parent / ".github" / "actions" / "check-dist" / "action.yml"

_spec = importlib.util.spec_from_file_location(
    "release_version", MANIFEST.parent / "scripts" / "release_version.py"
)
release_version = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release_version
_spec.loader.exec_module(release_version)
OPENAPI_SHA256 = (
    "abe76bc8feadd85462ff4293eba9bc1b2cea44b9929b0fbc772a120d84efb365"
)
OPENAPI_RELATIVE_PATH = (
    "contracts/relay-server/openapi.yaml"
)

KNOWN_MANIFEST_FIELDS = {
    "name", "version", "description", "author", "requires_env",
    "provides_tools", "provides_hooks", "kind", "hooks", "label",
    "optional_env", "platforms", "external_dependencies", "pip_dependencies",
    "provides_browser_providers", "provides_web_providers",
    "manifest_version", "api_version", "requires_plugins",
    "python_dependencies", "config_schema", "license", "homepage", "tags",
    "capabilities", "emits", "listens", "hermes", "depends",
    "requires_hermes", "python_runtime",
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
    # The floor Hermes enforces itself, with the version it reports.
    assert manifest["requires_hermes"] == ">=0.19.0"
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
    assert len(raw) == 361290
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


def test_current_contract_omits_anonymous_registration():
    root = MANIFEST.parent
    current = yaml.safe_load((root / OPENAPI_RELATIVE_PATH).read_bytes())
    assert "post" not in current["paths"].get("/v1/agents", {})


def test_hosted_workflows_pin_every_external_action_to_a_sha():
    uses_pattern = re.compile(r"^\s*uses:\s*([^#\s]+)", re.MULTILINE)
    sha_pattern = re.compile(r"^[^@]+@[0-9a-f]{40}$")
    for path in [*sorted(WORKFLOWS.glob("*.yml")), CHECK_ACTION]:
        text = path.read_text(encoding="utf-8")
        uses = uses_pattern.findall(text)
        assert uses or path == CHECK_ACTION, path.name
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


INFISICAL_STEP = "Load the PyPI API token from Infisical"


def assert_token_from_infisical(job, text, name):
    # The org's CI pattern: GitHub OIDC to the shared Infisical identity, the
    # token at relay-yscy / prod / /ci/pypi. No repository secret is read.
    assert "secrets.PYPI_API_TOKEN" not in text, name
    assert job["permissions"]["id-token"] == "write", name
    names = [step.get("name") for step in job["steps"]]
    loader = job["steps"][names.index(INFISICAL_STEP)]
    assert loader["uses"] == "Infisical/secrets-action@8a06c1bdcd5b8635d510c52d4b57a92c1ccef785", name
    assert loader["with"] == {
        "method": "oidc",
        "identity-id": "35c5afed-649f-4b2d-8e73-cf70338b3994",
        "project-slug": "relay-yscy",
        "env-slug": "prod",
        "secret-path": "/ci/pypi",
    }, name
    first_use = min(
        i for i, step in enumerate(job["steps"])
        if "PYPI_API_TOKEN" in yaml.safe_dump(step.get("with", {}))
    )
    assert names.index(INFISICAL_STEP) < first_use, name


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
    # with the PyPI API token, loaded from Infisical over OIDC and never a
    # literal in the tracked workflow or a repository secret.
    assert_token_from_infisical(jobs["publish"], text, "publish-rc.yml")
    assert "password: ${{ env.PYPI_API_TOKEN }}" in text
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
    assert 'python-version: ["3.11", "3.12", "3.13", "3.14"]' in text
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
    assert text.count('python scripts/release_version.py show | tee -a "$GITHUB_ENV"') == 3
    assert 'os.environ["EXPECTED_MANIFEST_VERSION"]' in text
    assert set(jobs) == {
        "build", "test", "published-hermes", "staging-bump-dry-run", "release-dry-run",
        "state-on-each-os", "hermes-install-scan",
    }
    assert jobs["state-on-each-os"]["strategy"]["matrix"]["os"] == ["macos-latest", "windows-latest"]
    assert "python scripts/release_version.py staging --dry-run" in text
    assert "python scripts/release_version.py release --dry-run" in text
    assert "grep -q '^release plan only: skip$' plan-b.txt" in text
    assert text.count('run: test -z "$(git status --porcelain)"') == 2


def test_release_lanes_share_one_check_step_and_identical_gated_uploads():
    # The check is a composite action of shell steps only. pypa's publish
    # action is a Docker action that names its image after the repository it
    # runs from; inside a composite that became RelayMessenger/Relay-Hermes
    # and Docker refused it (run 34290221859), so the composite may never
    # `uses:` anything.
    action = CHECK_ACTION.read_text(encoding="utf-8")
    steps = yaml.safe_load(action)["runs"]["steps"]
    assert all("uses" not in step and step["shell"] == "bash" for step in steps)
    assert "python scripts/release_version.py verify-dist" in action
    assert "sha256sum --check provenance/SHA256SUMS" in action
    assert "PYPI_TRUSTED_PUBLISHING must be unset or exactly 'true'" in action
    assert not re.search(r"pypi-[A-Za-z0-9_-]{16,}", action)

    lanes = {
        "publish-staging.yml": ("staging", "pypi-staging", "publish", ""),
        "release.yml": (
            "main",
            "pypi-release",
            "release",
            "github.event_name == 'push' && steps.plan.outputs.publish == 'true' && ",
        ),
    }
    uploads = {}
    for name, (branch, environment, job_name, gate) in lanes.items():
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        parsed = yaml.safe_load(text)
        assert parsed[True]["push"]["branches"] == [branch], name
        job = parsed["jobs"][job_name]
        assert job["environment"] == environment, name
        assert job["permissions"]["id-token"] == "write", name
        check = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/check-dist")
        assert check["with"]["trusted-publishing"] == "${{ vars.PYPI_TRUSTED_PUBLISHING }}", name
        assert check["with"]["token"] == "${{ env.PYPI_API_TOKEN }}", name
        assert_token_from_infisical(job, text, name)
        loader = next(step for step in job["steps"] if step.get("name") == INFISICAL_STEP)
        assert loader["if"] == gate + "vars.PYPI_TRUSTED_PUBLISHING != 'true'", name
        publishers = [
            step for step in job["steps"] if "pypa/gh-action-pypi-publish" in step.get("uses", "")
        ]
        assert len(publishers) == 2, name
        token, oidc = publishers
        assert token["if"] == gate + "vars.PYPI_TRUSTED_PUBLISHING != 'true'", name
        assert oidc["if"] == gate + "vars.PYPI_TRUSTED_PUBLISHING == 'true'", name
        # The two lanes carry the same upload steps, word for word, so one
        # variable flips both.
        uploads[name] = [(step["name"], step["uses"], step["with"]) for step in publishers]
        assert "github.repository == 'RelayMessenger/Relay-Hermes'" in text, name
        assert "python scripts/release_version.py verify-published" in text, name
        assert not re.search(r"pypi-[A-Za-z0-9_-]{16,}", text), name
    assert uploads["publish-staging.yml"] == uploads["release.yml"]
    (_, _, token_with), (_, _, oidc_with) = uploads["release.yml"]
    assert token_with["password"] == "${{ env.PYPI_API_TOKEN }}"
    assert token_with["attestations"] is False
    assert "password" not in oidc_with
    assert oidc_with["attestations"] is True

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
    assert "forbid: ${{ steps.plan.outputs.staging_version }}" in release
    assert "refs/tags/${RELEASE_TAG}" in release


@pytest.mark.parametrize("name", ["plugin.yaml", "README.md"])
def test_shipped_copy_carries_no_em_dashes(name):
    text = (MANIFEST.parent / name).read_text(encoding="utf-8")
    assert "\u2014" not in text


def test_python_314_joins_hermes_dependency_workspace():
    """Hermes's installer ships Python 3.14.7, and PM refuses a member whose
    requires-python excludes it ("requires Python >=3.11,<3.14, but Hermes
    runs on Python 3.14.7"): uv intersects every member's requires-python."""
    from packaging.specifiers import SpecifierSet

    metadata = tomllib.loads((MANIFEST.parent / "pyproject.toml").read_text(encoding="utf-8"))
    requires = metadata["project"]["requires-python"]
    for version in ("3.11.0", "3.12.0", "3.13.0", "3.14.7"):
        assert SpecifierSet(requires).contains(version), (requires, version)
    assert "Programming Language :: Python :: 3.14" in metadata["project"]["classifiers"]


# Hermes copies the whole plugin into its dependency workspace before it
# resolves it: <HERMES_HOME>\installs\<16>\environments\<32>\workspace\
# plugin-sources\relay-hermes-<16>\ (pm/workspace.py _workspace_member). Under
# the Windows SYSTEM profile that prefix measured 189 characters (Daytona,
# 2026-09-26), and Windows refuses a directory past 248 and a file past 260
# (MAX_PATH), which surfaced as WinError 206 at 265 characters.
WINDOWS_MEMBER_PREFIX = 189
WINDOWS_MAX_PATH = 260


def test_every_tracked_path_fits_hermes_windows_workspace():
    import subprocess

    # What `hermes plugins install` clones: the tracked tree. CI keeps other
    # checkouts (_hermes-agent) and build output beside it, untracked.
    root = MANIFEST.parent
    listed = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True,
    )
    paths = listed.stdout.split()
    assert paths
    longest = max(paths, key=len)
    assert WINDOWS_MEMBER_PREFIX + 1 + len(longest) < WINDOWS_MAX_PATH - 12, longest


def test_documented_manual_install_never_prompts():
    """``install --enable`` asks before preparing Python dependencies and,
    without a terminal, skips them and leaves the plugin disabled
    (hermes_cli/plugins_cmd_install.py _consent_python_deps). ``--no-enable``
    then ``enable`` is Hermes's non-interactive path."""
    readme = (MANIFEST.parent / "README.md").read_text(encoding="utf-8")
    assert "hermes plugins install RelayMessenger/Relay-Hermes --no-enable\nhermes plugins enable relay-hermes\n" in readme
    assert "hermes plugins install RelayMessenger/Relay-Hermes --enable" not in readme


def test_readme_installs_the_released_relay_cli():
    """main is staging's exact tree and no promotion step rewrites the README
    (release.yml publishes it verbatim as the PyPI long description), so the
    README names the released CLI, never an npm staging tag or prerelease.
    ``npx relaymessenger connect hermes`` resolves the ``latest`` dist-tag."""
    readme = (MANIFEST.parent / "README.md").read_text(encoding="utf-8")
    assert "npx relaymessenger connect hermes" in readme
    staging_spec = re.compile(
        r"(?:relaymessenger|@relaymessenger/[a-z-]+|relay-claude-channel)@staging\b"
        r"|\b\d+\.\d+\.\d+-staging\.\d+\b"
    )
    assert staging_spec.findall(readme) == []
