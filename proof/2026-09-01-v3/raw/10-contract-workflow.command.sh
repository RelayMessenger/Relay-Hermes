set -euo pipefail
cd /home/daytona/relay-hermes
.venv-3.13/bin/python scripts/check-openapi.py /home/daytona/relay-openapi.yaml
sh -n scripts/run-staging.sh
set +e
env -u RELAY_AGENT_TOKEN sh scripts/run-staging.sh >/tmp/no-token.out 2>&1
no_token_rc=$?
RELAY_AGENT_TOKEN=dummy RELAY_BASE_URL=https://api.relayapp.im \
  sh scripts/run-staging.sh >/tmp/wrong-origin.out 2>&1
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
adapter = Path("adapter.py").read_text()
manifest = Path("plugin.yaml").read_text()
state = Path("state.py").read_text()
assert "RELAY_ALLOW_ALL_CONTACTS" not in adapter
assert 'os.getenv("RELAY_' not in adapter
assert "RELAY_OPERATOR_CONTACTS" not in adapter
assert "RELAY_OPERATOR_CONTACTS" not in manifest
assert "allow_update_command=False" in adapter
assert "def _is_slash_message" in adapter
assert "sqlite3.connect(self.path" not in state
assert "file:{self._database_descriptor_path()}?mode=rw" in state
print("slash_static=pass operator_config=false relay_update=false adapter_dispatch=false")
print("security_static=pass profile_scope=true pathname_sqlite_connect=false")
print("artifact_lineage_static=pass one_distribution_pair=true exact_download_rehash_publish=true")
PY
(
  cd release
  sha256sum --check provenance/SHA256SUMS
  test "$(find dist -maxdepth 1 -type f | wc -l)" -eq 2
)
