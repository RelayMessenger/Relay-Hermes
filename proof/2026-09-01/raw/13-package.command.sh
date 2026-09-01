set -euo pipefail
.venv-3.13/bin/python - <<'PY'
from pathlib import Path
import shutil
for name in ("build", "dist", "relay_hermes.egg-info"):
    path = Path(name)
    if path.exists():
        shutil.rmtree(path)
PY
.venv-3.13/bin/python -m build
sha256sum dist/*
test -f dist/relay_hermes-1.0.0rc1-py3-none-any.whl
test -f dist/relay_hermes-1.0.0rc1.tar.gz
unzip -l dist/relay_hermes-1.0.0rc1-py3-none-any.whl | grep relay_hermes/plugin.yaml
tar -tzf dist/relay_hermes-1.0.0rc1.tar.gz | grep -E '/(plugin.yaml|scripts/check-openapi.py|scripts/run-staging.sh|tests/conftest.py)$'
for py in 3.11 3.12 3.13; do
  wheel="/home/daytona/final-wheel-$py"
  sdist="/home/daytona/final-sdist-$py"
  uv venv --python "$py" "$wheel"
  uv venv --python "$py" "$sdist"
  uv pip install --python "$wheel/bin/python" dist/relay_hermes-1.0.0rc1-py3-none-any.whl "PyYAML==6.0.3" "rich==15.0.0" "requests>=2"
  uv pip install --python "$sdist/bin/python" dist/relay_hermes-1.0.0rc1.tar.gz
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
done
