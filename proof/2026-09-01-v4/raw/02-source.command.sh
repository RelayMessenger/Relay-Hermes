set -euo pipefail
SOURCE_COMMIT=25d35866b84efeeaf8c9858db148ca16553aded4
AUDITED_HEAD=6e2f06d7da73c1430a7100485b279eae565c37a9
SANDBOX_ID=4443c705-f35c-4172-839e-1520e2c64d52
LOCKED_INPUT=/Users/advaitpaliwal/Code/Relay/_runtime/relay-openapi-locked-9b4d5bb.yaml
SNAPSHOT=contracts/relay-server/9b4d5bb32cc749c6fd271969948c385300d404d6/openapi.yaml
test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"
git diff --quiet "$AUDITED_HEAD" "$SOURCE_COMMIT" -- \
  __init__.py adapter.py plugin.yaml pyproject.toml relay_api.py \
  scripts/check-openapi.py scripts/run-staging.sh state.py MANIFEST.in
echo runtime_and_package_behavior_source=unchanged
git diff --name-only "$AUDITED_HEAD" "$SOURCE_COMMIT"
cmp -s "$LOCKED_INPUT" "$SNAPSHOT"
echo locked_workspace_bytes=exact
shasum -a 256 "$LOCKED_INPUT" "$SNAPSHOT"
git ls-tree -r --name-only "$SOURCE_COMMIT" |
  LC_ALL=C sort |
  grep -v '^proof/' > /tmp/hermes-contract-v4-source-files.txt
while IFS= read -r file; do
  git diff --quiet "$SOURCE_COMMIT" -- "$file"
  shasum -a 256 "$file"
done < /tmp/hermes-contract-v4-source-files.txt > /tmp/hermes-contract-v4-source.sha256
cp /tmp/hermes-contract-v4-source.sha256 proof/2026-09-01-v4/raw/source.sha256
COPYFILE_DISABLE=1 tar -czf /tmp/hermes-contract-v4-source.tar.gz \
  -T /tmp/hermes-contract-v4-source-files.txt
printf 'source_commit=%s\n' "$SOURCE_COMMIT"
printf 'source_files='; wc -l < /tmp/hermes-contract-v4-source-files.txt
shasum -a 256 /tmp/hermes-contract-v4-source.sha256
shasum -a 256 /tmp/hermes-contract-v4-source.tar.gz
cat /tmp/hermes-contract-v4-source.sha256
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  "$SANDBOX_ID" /tmp/hermes-contract-v4-source.tar.gz \
  /home/daytona/relay-hermes-contract-v4-source.tar.gz
