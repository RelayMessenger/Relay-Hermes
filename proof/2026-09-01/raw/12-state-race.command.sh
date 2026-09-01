set -euo pipefail
.venv-3.13/bin/python -m pytest -q tests/test_state.py -k 'permissive or symlink or replacement'
