#!/bin/sh
set -eu

: "${RELAY_AGENT_TOKEN:?Set a staging RELAY_AGENT_TOKEN first.}"

export RELAY_BASE_URL="${RELAY_BASE_URL:-https://api.staging.relayapp.im}"
export RELAY_STATE_DIR="${RELAY_STATE_DIR:-$HOME/.hermes/relay-staging}"

case "$RELAY_BASE_URL" in
  https://api.staging.relayapp.im) ;;
  *)
    printf '%s\n' \
      "Refusing to start: RELAY_BASE_URL must be https://api.staging.relayapp.im" >&2
    exit 2
    ;;
esac

# RelayInbox owns state-directory creation, no-follow validation, account
# binding, and mode enforcement after Hermes loads the adapter.
exec hermes gateway run
