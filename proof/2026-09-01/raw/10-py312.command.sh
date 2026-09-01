set -euo pipefail
export HERMES_AGENT_SRC=/home/daytona/hermes-agent
.venv-3.12/bin/python --version
.venv-3.12/bin/python -m pytest -q
