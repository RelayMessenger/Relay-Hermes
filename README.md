# Relay-Hermes

[Relay-Hermes](https://github.com/RelayMessenger/Relay-Hermes) connects
[Hermes Agent](https://github.com/NousResearch/hermes-agent) to Relay as an
always-on messaging platform.

Relay events arrive over the Relay v1 acknowledged WebSocket. Hermes replies
are sent through Relay's REST API.

## Delivery model

For each Relay event, the plugin:

1. commits the complete envelope and `event_id` to a durable SQLite inbox;
2. sends a cumulative WebSocket ACK only after the commit;
3. deduplicates replayed `event_id` values;
4. starts a Hermes turn only for an inbound `message.received` event;
5. explicitly marks the Chat Read when Hermes starts processing the turn;
6. sends each Message through `POST /v1/chats/{chatId}/messages` with an
   `Idempotency-Key`.

Transport acknowledgement and Read are separate. A WebSocket ACK never marks
a Chat Read.

Relay-Hermes does not poll for events and does not expose a public HTTP server.
It also does not add reactions, edits, typing indicators, or other Message
effects.

### Recovery

The SQLite inbox survives gateway restarts. If processing fails, the event
returns to `pending`; the durable transport checkpoint does not move backward.

If Relay reports that a checkpoint is outside retention, the plugin follows
the WebSocket FULL-sync flow. It pages through visible Chats and Messages,
validates the snapshot, atomically stores it with `full_sync_through`, and
sends `full_sync_complete` only after the transaction commits. Historical
Messages rebuild local indexes and never become new Hermes turns.

## Install

Hermes supports Git-installed directory plugins:

```sh
hermes plugins install RelayMessenger/Relay-Hermes --enable
```

The same repository can be installed as a Python package. Its
`hermes_agent.plugins` entry point registers the identical platform adapter.

Create an Agent and copy its Agent Token from
[Relay Console](https://console.relayapp.im), then save the token in
`~/.hermes/.env`:

```dotenv
RELAY_AGENT_TOKEN=your_agent_token
```

An Agent using this WebSocket must not have a saved webhook subscription.
Relay rejects the WebSocket upgrade with HTTP `409` until those subscriptions
are removed.

Start Hermes in the foreground:

```sh
hermes gateway run
```

For an always-on installation, use Hermes's service commands:

```sh
hermes gateway install
hermes gateway start
```

The Hermes platform id is `relayapp`. Hermes already reserves `relay` for its
generic connector platform.

## Configuration

`RELAY_AGENT_TOKEN` is the only required setting.

| Setting | Default | Meaning |
| --- | --- | --- |
| `RELAY_BASE_URL` | `https://api.relayapp.im` | Relay API origin |
| `RELAY_ALLOWED_CONTACTS` | all reachable Contacts | Comma-separated Contact ids allowed to start turns |
| `RELAY_STATE_DIR` | `~/.hermes/relay` | Durable SQLite inbox directory |
| `RELAY_REPLY_TO_MODE` | `auto` | Reply anchor policy: `off`, `first`, `all`, or `auto` |
| `RELAY_GROUP_CHAT_POLICY` | `mentions` | Group Chat policy: `mentions` or `all` |
| `RELAY_HOME_CHAT` | unset | Chat id for cron and direct `hermes send` delivery |
| `RELAY_HOME_CHAT_NAME` | Chat id | Human label for the home Chat |

Non-secret settings can instead be placed under
`gateway.platforms.relayapp.extra` in `~/.hermes/config.yaml`; environment
variables take precedence:

```yaml
gateway:
  platforms:
    relayapp:
      enabled: true
      extra:
        allowed_contacts:
          - 01993d50-ef7b-7b37-886b-23fd80c7ec12
        group_chat_policy: mentions
        reply_to_mode: auto
```

### Isolated staging

Use a staging Agent Token, the staging API origin, and a separate inbox:

```sh
export RELAY_AGENT_TOKEN='staging-agent-token'
export RELAY_BASE_URL='https://api.staging.relayapp.im'
export RELAY_STATE_DIR="$HOME/.hermes/relay-staging"
./scripts/run-staging.sh
```

The staging helper refuses every other API origin.

## Locked Relay contract

This version was audited against Relay Server developer OpenAPI commit
`9b4d5bb32cc749c6fd271969948c385300d404d6`. The exact
`contracts/developer/openapi.yaml` SHA-256 is
`f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`.

The runtime contract used here is:

- `GET /v1/websocket` for acknowledged agent events;
- `GET /v1/chats` and `GET /v1/chats/{chatId}/messages` only for a
  server-directed FULL sync;
- `POST /v1/chats/{chatId}/read` with no request body at processing start;
- `POST /v1/attachments`, followed by the allocated raw `PUT`, for local
  files;
- `POST /v1/chats/{chatId}/messages` with `Idempotency-Key` for every Message
  send;
- event envelopes with `api_version: v1` and
  `webhook_version: 2026-08-30`.

Relay vocabulary in the adapter is Contact, Handle, Chat, Message, and
Participant.

## Development

Python 3.11 through 3.13 are supported.

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
HERMES_AGENT_SRC=/path/to/hermes-agent .venv/bin/python -m pytest
python -m build
hermes plugins doctor . --ci
```

Tests cover REST paths and bodies, Message idempotency, explicit Read timing,
FULL-sync recovery, WebSocket authentication and acknowledgement ordering,
replay deduplication, heartbeat and reconnect behavior, package installation,
and Hermes directory-plugin registration.
