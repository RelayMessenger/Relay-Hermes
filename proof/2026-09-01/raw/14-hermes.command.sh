set -euo pipefail
for py in 3.11 3.12 3.13; do
  printf 'plugin_doctor_python=%s\n' "$py"
  (cd /home/daytona/hermes-agent && PYTHONPATH=/home/daytona/hermes-agent "/home/daytona/relay-hermes/.venv-$py/bin/python" - <<'PY'
from hermes_cli.plugins_cmd import cmd_plugin_doctor
cmd_plugin_doctor("/home/daytona/relay-hermes", ci=True)
PY
  )
  home="/home/daytona/hermes-wheel-$py"
  mkdir -p "$home"
  printf 'plugins:\n  enabled:\n    - relay-hermes\n' > "$home/config.yaml"
  (cd /home/daytona/hermes-agent && \
    HERMES_HOME="$home" \
    RELAY_AGENT_TOKEN=relay-test-token \
    RELAY_BASE_URL=https://api.staging.relayapp.im \
    RELAY_STATE_DIR="$home/relay-state" \
    PYTHONPATH=/home/daytona/hermes-agent \
    "/home/daytona/final-wheel-$py/bin/python" - <<'PY'
import sys
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
print(f"wheel_runtime=pass python={sys.version.split()[0]} plugin=relay-hermes platform=relayapp")
PY
  )
done
