set -euo pipefail
SOURCE_COMMIT=0e8c82e713d127cc943b60d9e6351fe1ea89e075
SANDBOX_ID=69ece5c1-7bd1-4d80-980c-c0d609c99833
test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"
git ls-tree -r --name-only "$SOURCE_COMMIT" |
  LC_ALL=C sort |
  grep -v '^proof/' > /tmp/hermes-security-v2-source-files.txt
test "$(wc -l < /tmp/hermes-security-v2-source-files.txt | tr -d ' ')" = 21
while IFS= read -r file; do
  git diff --quiet "$SOURCE_COMMIT" -- "$file"
  shasum -a 256 "$file"
done < /tmp/hermes-security-v2-source-files.txt > /tmp/hermes-security-v2-source.sha256
cp /tmp/hermes-security-v2-source.sha256 proof/2026-09-01-v2/raw/source.sha256
COPYFILE_DISABLE=1 tar -czf /tmp/hermes-security-v2-source.tar.gz \
  -T /tmp/hermes-security-v2-source-files.txt
printf 'source_commit=%s\n' "$SOURCE_COMMIT"
printf 'source_files='; wc -l < /tmp/hermes-security-v2-source-files.txt
shasum -a 256 /tmp/hermes-security-v2-source.sha256
shasum -a 256 /tmp/hermes-security-v2-source.tar.gz
cat /tmp/hermes-security-v2-source.sha256
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  "$SANDBOX_ID" \
  /tmp/hermes-security-v2-source.tar.gz \
  /home/daytona/relay-hermes-security-v2-source.tar.gz
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  "$SANDBOX_ID" \
  /tmp/relay-hermes-openapi-9b4d5bb.yaml \
  /home/daytona/relay-openapi.yaml
