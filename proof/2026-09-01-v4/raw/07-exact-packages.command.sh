set -euo pipefail
cd /home/daytona/relay-hermes
(
  cd release
  sha256sum --check provenance/SHA256SUMS
)
for py in 3.11 3.12 3.13; do
  wheel="/home/daytona/contract-v4-wheel-$py"
  sdist="/home/daytona/contract-v4-sdist-$py"
  uv venv --python "$py" "$wheel"
  uv venv --python "$py" "$sdist"
  uv pip install --python "$wheel/bin/python" \
    release/dist/relay_hermes-1.0.0rc1-py3-none-any.whl \
    "PyYAML==6.0.3" "rich==15.0.0" "requests>=2"
  uv pip install --python "$sdist/bin/python" \
    release/dist/relay_hermes-1.0.0rc1.tar.gz
  uv pip check --python "$wheel/bin/python"
  uv pip check --python "$sdist/bin/python"
  for clean in "$wheel" "$sdist"; do
    (cd /tmp && EXPECTED_SOURCE_ROOT=/home/daytona/relay-hermes "$clean/bin/python" - <<'PY'
import hashlib
import os
import sys
from importlib.metadata import entry_points, version
from importlib.resources import files
from pathlib import Path
installed = version("relay-hermes")
assert installed == "1.0.0rc1"
entrypoint = next(
    item for item in entry_points().select(group="hermes_agent.plugins")
    if item.name == "relay-hermes"
)
assert entrypoint.value == "relay_hermes"
assert callable(entrypoint.load().register)
source = Path(os.environ["EXPECTED_SOURCE_ROOT"])
package = files("relay_hermes")
for name in ("__init__.py", "adapter.py", "plugin.yaml", "relay_api.py", "state.py"):
    expected = hashlib.sha256((source / name).read_bytes()).hexdigest()
    actual = hashlib.sha256(package.joinpath(name).read_bytes()).hexdigest()
    assert actual == expected, name
print(f"clean_install=pass python={sys.version.split()[0]} version={installed} runtime_payload=exact")
PY
    )
  done
done
doctor_root=/home/daytona/contract-v4-doctor-3.13
mkdir -p "$doctor_root"
tar -xzf release/dist/relay_hermes-1.0.0rc1.tar.gz -C "$doctor_root"
(cd /home/daytona/hermes-agent && \
  SDIST_PLUGIN_DIR="$doctor_root/relay_hermes-1.0.0rc1" \
  PYTHONPATH=/home/daytona/hermes-agent \
  /home/daytona/relay-hermes/.venv-3.13/bin/python - <<'PY'
import os
from hermes_cli.plugins_cmd import cmd_plugin_doctor
cmd_plugin_doctor(os.environ["SDIST_PLUGIN_DIR"], ci=True)
PY
)
hermes_home=/home/daytona/contract-v4-hermes-wheel-3.13
mkdir -p "$hermes_home"
printf 'plugins:\n  enabled:\n    - relay-hermes\n' > "$hermes_home/config.yaml"
(cd /home/daytona/hermes-agent && \
  HERMES_HOME="$hermes_home" \
  RELAY_AGENT_TOKEN=relay-test-token \
  RELAY_BASE_URL=https://api.staging.relayapp.im \
  RELAY_STATE_DIR="$hermes_home/relay-state" \
  PYTHONPATH=/home/daytona/hermes-agent \
  /home/daytona/contract-v4-wheel-3.13/bin/python - <<'PY'
from gateway.platform_registry import platform_registry
from hermes_cli.plugins import PluginManager
manager = PluginManager()
manager.discover_and_load()
plugin = next(
    item for item in manager.list_plugins()
    if item["name"] == "relay-hermes" and item["source"] == "entrypoint"
)
entry = platform_registry.get("relayapp")
assert plugin["enabled"] is True and plugin["error"] is None
assert entry is not None and entry.required_env == ["RELAY_AGENT_TOKEN"]
assert entry.allow_update_command is False
print("exact_wheel_hermes_load=pass python=3.13 package_behavior=unchanged")
PY
)
(
  cd release
  sha256sum --check provenance/SHA256SUMS
  test "$(find dist -maxdepth 1 -type f | wc -l)" -eq 2
)
tar -czf /home/daytona/relay-hermes-contract-v4-artifacts.tar.gz -C release dist provenance
sha256sum release/dist/* /home/daytona/relay-hermes-contract-v4-artifacts.tar.gz
