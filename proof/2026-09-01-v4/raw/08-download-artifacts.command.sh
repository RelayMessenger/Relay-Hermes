set -euo pipefail
SANDBOX_ID=4443c705-f35c-4172-839e-1520e2c64d52
ARCHIVE_SHA256=01ad8f34115709c1a62a200ab1eea11a649e845c89fb9d408f9b4d33617dd49b
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/download-file.mjs \
  "$SANDBOX_ID" /home/daytona/relay-hermes-contract-v4-artifacts.tar.gz \
  /tmp/relay-hermes-contract-v4-artifacts.tar.gz
test "$(shasum -a 256 /tmp/relay-hermes-contract-v4-artifacts.tar.gz | cut -d' ' -f1)" = "$ARCHIVE_SHA256"
rm -rf /tmp/relay-hermes-contract-v4-artifacts
mkdir -p /tmp/relay-hermes-contract-v4-artifacts
tar -xzf /tmp/relay-hermes-contract-v4-artifacts.tar.gz \
  -C /tmp/relay-hermes-contract-v4-artifacts
(
  cd /tmp/relay-hermes-contract-v4-artifacts
  shasum -a 256 -c provenance/SHA256SUMS
  shasum -a 256 dist/*
)
cp /tmp/relay-hermes-contract-v4-artifacts/dist/* proof/2026-09-01-v4/artifacts/
shasum -a 256 proof/2026-09-01-v4/artifacts/* > proof/2026-09-01-v4/raw/artifacts.sha256
cat proof/2026-09-01-v4/raw/artifacts.sha256
