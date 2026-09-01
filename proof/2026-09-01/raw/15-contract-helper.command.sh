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
from pathlib import Path
import yaml
canonical = "RelayMessenger/Relay-Hermes"
sha = re.compile(r"^[^@]+@[0-9a-f]{40}$")
for path in sorted(Path(".github/workflows").glob("*.yml")):
    text = path.read_text()
    data = yaml.safe_load(text)
    assert "jobs" in data
    for value in re.findall(r"^\s*uses:\s*([^#\s]+)", text, re.MULTILINE):
        assert value.startswith("./") or sha.fullmatch(value), value
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            assert "${{" not in step.get("run", "")
    print(f"workflow_static=pass file={path}")
publish = Path(".github/workflows/publish-rc.yml").read_text()
assert canonical in publish
assert "hermes-relay-plugin" not in publish
assert "RELAY_CONTRACT_READ_TOKEN" in publish
assert "attest-build-provenance@" in publish
assert "uses: ./.github/workflows/ci.yml" in publish
print("release_chain_static=pass canonical_repo=RelayMessenger/Relay-Hermes")
PY
