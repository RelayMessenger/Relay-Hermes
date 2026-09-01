set -euo pipefail
.venv-3.13/bin/python scripts/check-openapi.py /home/daytona/relay-openapi.yaml
sh -n scripts/run-staging.sh
set +e
env -u RELAY_AGENT_TOKEN sh scripts/run-staging.sh >/tmp/no-token.out 2>&1
no_token_rc=$?
RELAY_AGENT_TOKEN=dummy RELAY_BASE_URL=https://api.relayapp.im sh scripts/run-staging.sh >/tmp/wrong-origin.out 2>&1
wrong_origin_rc=$?
set -e
cat /tmp/no-token.out
cat /tmp/wrong-origin.out
test "$no_token_rc" -ne 0
test "$wrong_origin_rc" -eq 2
printf 'staging_helper=pass no_token_rc=%s wrong_origin_rc=%s\n' "$no_token_rc" "$wrong_origin_rc"
.venv-3.13/bin/python - <<'PY'
import re
import tomllib
from pathlib import Path
import yaml
sha = re.compile(r"^[^@]+@[0-9a-f]{40}$")
for path in sorted(Path(".github/workflows").glob("*.yml")):
    text = path.read_text()
    data = yaml.safe_load(text)
    for value in re.findall(r"^\s*uses:\s*([^#\s]+)", text, re.MULTILINE):
        assert value.startswith("./") or sha.fullmatch(value), value
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", "")
    print(f"workflow_static=pass file={path}")
ci_text = Path(".github/workflows/ci.yml").read_text()
ci = yaml.safe_load(ci_text)["jobs"]
assert ci["test"]["needs"] == "build"
assert ci_text.count("python -m build --outdir release/dist") == 1
assert "Retain the distributions before validation" in ci_text
assert "Download the one CI-built distribution pair" in ci_text
publish_text = Path(".github/workflows/publish-rc.yml").read_text()
publish = yaml.safe_load(publish_text)["jobs"]
assert "python -m build" not in publish_text
assert publish["provenance"]["needs"] == "contract"
assert publish["publish"]["needs"] == "provenance"
assert publish_text.count("sha256sum --check provenance/SHA256SUMS") == 2
assert "packages-dir: release/dist" in publish_text
metadata = tomllib.loads(Path("pyproject.toml").read_text())
assert metadata["build-system"]["requires"] == ["setuptools==84.0.0"]
print("artifact_lineage_static=pass one_build=true exact_download_rehash_publish=true")
PY
