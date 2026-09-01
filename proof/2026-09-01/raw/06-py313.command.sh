set -euo pipefail
export HERMES_AGENT_SRC=/home/daytona/hermes-agent
.venv-3.13/bin/python --version
.venv-3.13/bin/python -m pytest -q
