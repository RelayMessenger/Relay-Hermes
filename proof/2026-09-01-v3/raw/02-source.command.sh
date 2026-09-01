set -euo pipefail
SOURCE_COMMIT=bb7185916e65c6af2cf319993a560024e8c649df
SANDBOX_ID=edd173f2-fb53-48c4-b5b1-c3e42fe6e7a5
test "$(git rev-parse HEAD)" = "$SOURCE_COMMIT"
git ls-tree -r --name-only "$SOURCE_COMMIT" |
  LC_ALL=C sort |
  grep -v '^proof/' > /tmp/hermes-slash-v3-source-files.txt
test "$(wc -l < /tmp/hermes-slash-v3-source-files.txt | tr -d ' ')" = 21
while IFS= read -r file; do
  git diff --quiet "$SOURCE_COMMIT" -- "$file"
  shasum -a 256 "$file"
done < /tmp/hermes-slash-v3-source-files.txt > /tmp/hermes-slash-v3-source.sha256
cp /tmp/hermes-slash-v3-source.sha256 proof/2026-09-01-v3/raw/source.sha256
COPYFILE_DISABLE=1 tar -czf /tmp/hermes-slash-v3-source.tar.gz \
  -T /tmp/hermes-slash-v3-source-files.txt
printf 'source_commit=%s\n' "$SOURCE_COMMIT"
printf 'source_files='; wc -l < /tmp/hermes-slash-v3-source-files.txt
shasum -a 256 /tmp/hermes-slash-v3-source.sha256
shasum -a 256 /tmp/hermes-slash-v3-source.tar.gz
cat /tmp/hermes-slash-v3-source.sha256
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  "$SANDBOX_ID" /tmp/hermes-slash-v3-source.tar.gz \
  /home/daytona/relay-hermes-slash-v3-source.tar.gz
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  "$SANDBOX_ID" /tmp/relay-hermes-openapi-9b4d5bb.yaml \
  /home/daytona/relay-openapi.yaml
