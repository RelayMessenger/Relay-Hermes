set -euo pipefail
sha256sum /home/daytona/relay-hermes-source-final.tar.gz
test "$(sha256sum /home/daytona/relay-hermes-source-final.tar.gz | cut -d' ' -f1)" = f6bde9187cb80f9d89357fcc726475b7575ec423f9f54ee80320a6b2c2acd6df
tar -xzf /home/daytona/relay-hermes-source-final.tar.gz -C /home/daytona/relay-hermes
cd /home/daytona/relay-hermes
tar -tzf /home/daytona/relay-hermes-source-final.tar.gz | LC_ALL=C sort > /tmp/relay-hermes-final-files.txt
while IFS= read -r file; do sha256sum "$file"; done < /tmp/relay-hermes-final-files.txt > /tmp/relay-hermes-final-source.sha256
sha256sum /tmp/relay-hermes-final-source.sha256
test "$(sha256sum /tmp/relay-hermes-final-source.sha256 | cut -d' ' -f1)" = 255afdf444bc7e96af86b9b64379d657d73581c597ad07a0af3a76ed2d80efae
for py in 3.11 3.12 3.13; do
  uv pip install --python ".venv-$py/bin/python" -e ".[dev]" "build==1.3.0" "rich==15.0.0" >/dev/null
  ".venv-$py/bin/python" --version
done
printf 'exact_source=pass files='; wc -l < /tmp/relay-hermes-final-files.txt
