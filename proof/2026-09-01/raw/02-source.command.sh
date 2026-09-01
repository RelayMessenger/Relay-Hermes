set -euo pipefail
git ls-files --cached --others --exclude-standard |
  LC_ALL=C sort |
  grep -v '^proof/' > /tmp/hermes-final-source-files.txt
while IFS= read -r file; do
  shasum -a 256 "$file"
done < /tmp/hermes-final-source-files.txt > /tmp/hermes-final-source.sha256
tar -czf /tmp/hermes-final-source.tar.gz \
  -T /tmp/hermes-final-source-files.txt
shasum -a 256 /tmp/hermes-final-source.sha256
shasum -a 256 /tmp/hermes-final-source.tar.gz
wc -l /tmp/hermes-final-source-files.txt
cat /tmp/hermes-final-source.sha256
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  d64bc9d6-8cae-4e86-9fa2-2ca454f34fd5 \
  /tmp/hermes-final-source.tar.gz \
  /home/daytona/relay-hermes-source.tar.gz
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  d64bc9d6-8cae-4e86-9fa2-2ca454f34fd5 \
  /tmp/relay-hermes-openapi-9b4d5bb.yaml \
  /home/daytona/relay-openapi.yaml
