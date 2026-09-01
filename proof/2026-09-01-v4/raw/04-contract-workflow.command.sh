set -euo pipefail
cd /home/daytona/relay-hermes
SNAPSHOT=contracts/relay-server/9b4d5bb32cc749c6fd271969948c385300d404d6/openapi.yaml
.venv-3.13/bin/python scripts/check-openapi.py "$SNAPSHOT"
.venv-3.13/bin/python -m pytest -q tests/test_plugin_manifest.py
.venv-3.13/bin/python - <<'PY'
import hashlib
import re
import tomllib
from pathlib import Path
import yaml
commit = "9b4d5bb32cc749c6fd271969948c385300d404d6"
digest = "f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5"
snapshot = Path(f"contracts/relay-server/{commit}/openapi.yaml")
raw = snapshot.read_bytes()
assert len(raw) == 117289
assert hashlib.sha256(raw).hexdigest() == digest
metadata = Path("contracts/relay-server/README.md").read_text()
assert commit in metadata and digest in metadata
assert "contracts/developer/openapi.yaml" in metadata
sha = re.compile(r"^[^@]+@[0-9a-f]{40}$")
for path in sorted(Path(".github/workflows").glob("*.yml")):
    text = path.read_text()
    data = yaml.safe_load(text)
    for value in re.findall(r"^\s*uses:\s*([^#\s]+)", text, re.MULTILINE):
        assert value.startswith("./") or sha.fullmatch(value), value
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", "")
    print(f"workflow_static=pass file={path} immutable_action_pins=true")
relative = str(snapshot)
ci_text = Path(".github/workflows/ci.yml").read_text()
ci = yaml.safe_load(ci_text)["jobs"]
assert ci["test"]["needs"] == "build"
assert ci_text.count("python -m build --outdir release/dist") == 1
assert ci_text.count(relative) == 1
assert 'python scripts/check-openapi.py "$RELAY_OPENAPI_SNAPSHOT"' in ci_text
assert ci_text.index("Validate the exact checked-in Relay contract snapshot") < ci_text.index("Build pinned wheel and sdist")
assert "Retain the distributions before validation" in ci_text
assert "Download the one CI-built distribution pair" in ci_text
publish_text = Path(".github/workflows/publish-rc.yml").read_text()
publish = yaml.safe_load(publish_text)["jobs"]
assert "python -m build" not in publish_text
assert publish["contract"]["needs"] == "validate"
assert publish["provenance"]["needs"] == "contract"
assert publish["publish"]["needs"] == "provenance"
assert publish_text.count(relative) == 1
assert 'python scripts/check-openapi.py "$RELAY_OPENAPI_SNAPSHOT"' in publish_text
assert "RELAY_CONTRACT_READ_TOKEN" not in publish_text
assert "RelayMessenger/Relay-Server" not in publish_text
assert "_relay-server" not in publish_text
contract_checkouts = [
    step for step in publish["contract"]["steps"]
    if str(step.get("uses", "")).startswith("actions/checkout@")
]
assert len(contract_checkouts) == 1
assert contract_checkouts[0]["with"]["repository"] == "RelayMessenger/Relay-Hermes"
assert publish_text.count("sha256sum --check provenance/SHA256SUMS") == 2
assert "packages-dir: release/dist" in publish_text
metadata_toml = tomllib.loads(Path("pyproject.toml").read_text())
assert metadata_toml["project"]["version"] == "1.0.0rc1"
assert metadata_toml["build-system"]["requires"] == ["setuptools==84.0.0"]
print("local_contract_snapshot=pass source_commit_metadata=true exact_hash=true")
print("private_checkout_and_token=absent")
print("artifact_lineage_static=pass one_distribution_pair=true exact_download_rehash_publish=true")
PY
