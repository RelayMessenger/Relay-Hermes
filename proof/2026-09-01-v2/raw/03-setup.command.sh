set -euo pipefail
SOURCE_ARCHIVE_SHA256=15d844e4feaf993537da387cdc068667fe422445ce0f44a35316f25abfbad230
SOURCE_MANIFEST_SHA256=4b69f80086089e30a793d6abcfb83c5aeeaa2e77265c015df1bb8c5ee5f73a9e
OPENAPI_SHA256=f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5
HERMES_COMMIT=04224b2f82aabbe89525451089eb2677edfae179
uname -a
cat /etc/os-release
printf 'architecture='; uname -m
sha256sum /home/daytona/relay-hermes-security-v2-source.tar.gz /home/daytona/relay-openapi.yaml
test "$(sha256sum /home/daytona/relay-hermes-security-v2-source.tar.gz | cut -d' ' -f1)" = "$SOURCE_ARCHIVE_SHA256"
test "$(sha256sum /home/daytona/relay-openapi.yaml | cut -d' ' -f1)" = "$OPENAPI_SHA256"
rm -rf /home/daytona/relay-hermes /home/daytona/hermes-agent
mkdir -p /home/daytona/relay-hermes
tar -xzf /home/daytona/relay-hermes-security-v2-source.tar.gz -C /home/daytona/relay-hermes
cd /home/daytona/relay-hermes
tar -tzf /home/daytona/relay-hermes-security-v2-source.tar.gz | LC_ALL=C sort > /tmp/security-v2-source-files.txt
while IFS= read -r file; do sha256sum "$file"; done < /tmp/security-v2-source-files.txt > /tmp/security-v2-source.sha256
sha256sum /tmp/security-v2-source.sha256
test "$(sha256sum /tmp/security-v2-source.sha256 | cut -d' ' -f1)" = "$SOURCE_MANIFEST_SHA256"
git clone --filter=blob:none --no-checkout https://github.com/NousResearch/hermes-agent.git /home/daytona/hermes-agent
git -C /home/daytona/hermes-agent fetch --depth=1 origin "$HERMES_COMMIT"
git -C /home/daytona/hermes-agent checkout --detach "$HERMES_COMMIT"
test "$(git -C /home/daytona/hermes-agent rev-parse HEAD)" = "$HERMES_COMMIT"
uv python install 3.11 3.12 3.13
for py in 3.11 3.12 3.13; do
  uv venv --python "$py" ".venv-$py"
  uv pip install --python ".venv-$py/bin/python" -e ".[dev]" \
    "build==1.3.0" "PyYAML==6.0.3" "rich==15.0.0" "requests>=2"
  ".venv-$py/bin/python" --version
done
printf 'exact_source=pass files='; wc -l < /tmp/security-v2-source-files.txt
