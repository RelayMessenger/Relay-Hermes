set -euo pipefail
test "$(shasum -a 256 /tmp/relay-hermes-openapi-9b4d5bb.yaml | cut -d' ' -f1)" = f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5
node /Users/advaitpaliwal/Code/Relay/_runtime/daytona-tools/upload-file.mjs \
  e4ebee80-2d36-473a-851b-a98d6b59f057 \
  /tmp/relay-hermes-openapi-9b4d5bb.yaml \
  /home/daytona/relay-openapi.yaml
