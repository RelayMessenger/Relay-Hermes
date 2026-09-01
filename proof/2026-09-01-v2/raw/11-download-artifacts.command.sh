set -euo pipefail
SANDBOX_ID=69ece5c1-7bd1-4d80-980c-c0d609c99833
PROOF=proof/2026-09-01-v2
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/download-file.mjs \
  "$SANDBOX_ID" \
  /home/daytona/relay-hermes/release/dist/relay_hermes-1.0.0rc1-py3-none-any.whl \
  "$PROOF/artifacts/relay_hermes-1.0.0rc1-py3-none-any.whl"
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/download-file.mjs \
  "$SANDBOX_ID" \
  /home/daytona/relay-hermes/release/dist/relay_hermes-1.0.0rc1.tar.gz \
  "$PROOF/artifacts/relay_hermes-1.0.0rc1.tar.gz"
(
  cd "$PROOF"
  shasum -a 256 artifacts/* | tee raw/artifacts.sha256
)
