set -euo pipefail
git ls-files --cached --others --exclude-standard |
  LC_ALL=C sort |
  grep -v '^proof/' > /tmp/relay-hermes-followup-source-final-files.txt
while IFS= read -r file; do
  shasum -a 256 "$file"
done < /tmp/relay-hermes-followup-source-final-files.txt \
  > /tmp/relay-hermes-followup-source-final.sha256
tar -czf /tmp/relay-hermes-followup-source-final.tar.gz \
  -T /tmp/relay-hermes-followup-source-final-files.txt
shasum -a 256 /tmp/relay-hermes-followup-source-final.sha256
shasum -a 256 /tmp/relay-hermes-followup-source-final.tar.gz
wc -l /tmp/relay-hermes-followup-source-final-files.txt
cat /tmp/relay-hermes-followup-source-final.sha256
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  e4ebee80-2d36-473a-851b-a98d6b59f057 \
  /tmp/relay-hermes-followup-source-final.tar.gz \
  /home/daytona/relay-hermes-source-final.tar.gz
