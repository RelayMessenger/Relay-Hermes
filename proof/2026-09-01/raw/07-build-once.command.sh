set -euo pipefail
.venv-3.13/bin/python - <<'PY'
from pathlib import Path
import shutil
for name in ("build", "release", "relay_hermes.egg-info"):
    path = Path(name)
    if path.exists():
        shutil.rmtree(path)
PY
.venv-3.13/bin/python - <<'PY'
import tomllib
from pathlib import Path
metadata = tomllib.loads(Path("pyproject.toml").read_text())
assert metadata["build-system"]["requires"] == ["setuptools==84.0.0"]
print("build_backend=setuptools==84.0.0 build_frontend=build==1.3.0")
PY
mkdir -p release/dist release/provenance
.venv-3.13/bin/python -m build --outdir release/dist
test -f release/dist/relay_hermes-1.0.0rc1-py3-none-any.whl
test -f release/dist/relay_hermes-1.0.0rc1.tar.gz
test "$(find release/dist -maxdepth 1 -type f | wc -l)" -eq 2
tar -tzf release/dist/relay_hermes-1.0.0rc1.tar.gz | grep -E '/(plugin.yaml|scripts/check-openapi.py|scripts/run-staging.sh|tests/test_staging_wrapper.py)$'
(
  cd release
  sha256sum dist/* > provenance/SHA256SUMS
  sha256sum --check provenance/SHA256SUMS
  cat provenance/SHA256SUMS
)
