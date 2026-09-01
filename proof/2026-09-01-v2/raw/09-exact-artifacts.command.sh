set -euo pipefail
cd /home/daytona/relay-hermes
(
  cd release
  sha256sum --check provenance/SHA256SUMS
)
for py in 3.11 3.12 3.13; do
  wheel="/home/daytona/security-v2-wheel-$py"
  sdist="/home/daytona/security-v2-sdist-$py"
  doctor_root="/home/daytona/security-v2-doctor-$py"
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
    (cd /tmp && "$clean/bin/python" - <<'PY'
import sys
from importlib.metadata import entry_points, version
installed = version("relay-hermes")
assert installed == "1.0.0rc1"
entrypoint = next(
    item
    for item in entry_points().select(group="hermes_agent.plugins")
    if item.name == "relay-hermes"
)
assert entrypoint.value == "relay_hermes"
assert callable(entrypoint.load().register)
print(f"clean_install=pass python={sys.version.split()[0]} version={installed}")
PY
    )
  done
  mkdir -p "$doctor_root"
  tar -xzf release/dist/relay_hermes-1.0.0rc1.tar.gz -C "$doctor_root"
  (cd /home/daytona/hermes-agent && \
    SDIST_PLUGIN_DIR="$doctor_root/relay_hermes-1.0.0rc1" \
    PYTHONPATH=/home/daytona/hermes-agent \
    "/home/daytona/relay-hermes/.venv-$py/bin/python" - <<'PY'
import os
from hermes_cli.plugins_cmd import cmd_plugin_doctor
cmd_plugin_doctor(os.environ["SDIST_PLUGIN_DIR"], ci=True)
PY
  )
  home="/home/daytona/security-v2-hermes-wheel-$py"
  mkdir -p "$home"
  printf 'plugins:\n  enabled:\n    - relay-hermes\n' > "$home/config.yaml"
  (cd /home/daytona/hermes-agent && \
    HERMES_HOME="$home" \
    RELAY_AGENT_TOKEN=relay-test-token \
    RELAY_BASE_URL=https://api.staging.relayapp.im \
    RELAY_STATE_DIR="$home/relay-state" \
    PYTHONPATH=/home/daytona/hermes-agent \
    "$wheel/bin/python" - <<'PY'
import sys
from gateway.config import PlatformConfig
from gateway.platform_registry import platform_registry
from gateway.slash_access import policy_from_extra
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
assert entry.allowed_users_env == "RELAY_ALLOWED_CONTACTS"
assert entry.allow_all_env == ""
config = PlatformConfig(extra={
    "token": "relay-test-token",
    "state_dir": __import__("os").environ["RELAY_STATE_DIR"],
})
adapter = entry.adapter_factory(config)
assert adapter._allowed_contacts == set()
assert adapter._operator_contacts == set()
assert policy_from_extra(config.extra, "dm").can_run("ordinary-contact", "update") is False
adapter._inbox.open().close()
print(f"exact_wheel_runtime=pass python={sys.version.split()[0]}")
PY
  )
done
sha256sum release/dist/*
