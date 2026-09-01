set -euo pipefail
cd /home/daytona/relay-hermes
HERMES_AGENT_SRC=/home/daytona/hermes-agent \
  .venv-3.13/bin/python -m pytest -q
