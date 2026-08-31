# hermes-relay-plugin

Connect [Hermes Agent](https://github.com/NousResearch/hermes-agent) to Relay
as an always-on agent.

The plugin receives agent events from Relay over WebSocket and sends replies
through Relay API v1.

## Reliability

For every event, the plugin:

1. commits the complete envelope and `event_id` to a durable SQLite inbox;
2. sends a cumulative ACK only after that commit;
3. processes the inbox after acceptance;
4. deduplicates replayed `event_id` values;
5. replies through `POST /v1/chats/{chatId}/messages`;
6. derives a stable `Idempotency-Key` from the triggering event.

The inbox survives a gateway restart. A processing failure returns the row to
`pending`; it does not move Relay's ACK backward or lose the event.

A cumulative WebSocket ACK, like a webhook HTTP 2xx, acknowledges transport
only. It never marks a Chat as read. The plugin calls Read only from Hermes's
processing-start hook for a turn Hermes actually begins.

When Relay reports that the saved checkpoint is outside retention, the plugin:

1. does not ACK any event;
2. paginates through every visible Chat and every page of its Messages;
3. validates Chat ids, Message ids, ownership, and pagination continuity;
4. atomically replaces a deterministic SQLite snapshot and saves the
   `full_sync_through` checkpoint in the same transaction;
5. sends `full_sync_complete` only after SQLite commits.

The snapshot rebuilds the adapter's Chat kind and latest-inbound indexes. It
does not turn old history into new agent turns. If the REST snapshot cannot be
safely reconciled, the plugin stops with `relay_full_sync_failed` and sends no
false completion.

## Setup

Create an agent and Agent Token in
[Relay Console](https://console.relayapp.im).

```sh
git clone https://github.com/relaymessenger/hermes-relay-plugin \
  ~/.hermes/plugins/relay
hermes plugins enable relayapp-platform
```

Set the Agent Token:

```sh
RELAY_AGENT_TOKEN=your_agent_token
```

The agent must have no saved webhook subscriptions. Relay returns HTTP `409`
when a webhook subscription exists. Delete the subscriptions before starting
Hermes with this plugin.

Then start Hermes:

```sh
hermes gateway start
```

The plugin connects to `wss://api.relayapp.im/v1/websocket`. It derives the
WebSocket URL from `RELAY_BASE_URL` and authenticates the upgrade with:

```http
Authorization: Bearer <Agent Token>
```

## Connection handling

The plugin replies to Relay heartbeat pings with `pong`. Heartbeats check the
connection; cumulative ACKs advance event delivery.

| Condition | Behavior |
| --- | --- |
| `heartbeat_timeout` or `restart` | Reconnect with exponential backoff and jitter |
| Retryable `ack_failed` or `delivery_failed` | Reconnect with backoff |
| Close code `1011`, `1012`, or `4408` | Reconnect with backoff |
| `revoked` or close code `4401` | Stop and require a valid Agent Token |
| HTTP `409`, `webhook_configured`, or close code `4410` | Stop until every webhook subscription is removed |
| Any other Relay policy close code | Stop and report the close reason |

## Configuration

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `RELAY_AGENT_TOKEN` | yes | | Agent Token |
| `RELAY_BASE_URL` | no | `https://api.relayapp.im` | Relay API origin |
| `RELAY_ALLOWED_USERS` | no | | Comma-separated Contact ids; unset accepts all |
| `RELAY_STATE_DIR` | no | `~/.hermes/relay` | SQLite inbox directory |
| `RELAY_REPLY_TO_MODE` | no | `auto` | `off`, `first`, `all`, or `auto` |
| `RELAY_GROUP_REPLY_POLICY` | no | `mentions` | `mentions` or `all` |
| `RELAY_HOME_CHANNEL` | no | | Chat id for cron delivery |
| `RELAY_HOME_CHANNEL_NAME` | no | | Human label for that Chat |

### Isolated staging

Use a staging Agent Token, a staging API origin, and a separate SQLite
directory. Never reuse a production token or checkpoint database:

```sh
export RELAY_AGENT_TOKEN='staging-agent-token'
export RELAY_BASE_URL='https://api.staging.relayapp.im'
export RELAY_STATE_DIR="$HOME/.hermes/relay-staging"
./scripts/run-staging.sh
```

## Current Relay contract

- event envelopes use `api_version: v1` and `webhook_version: 2026-08-30`;
- incoming Message data is the event's `data` object;
- Chat id is `data.chat.id`;
- sender is `data.sender_handle`;
- text is `part.value`;
- mentions use `part.mention`;
- Read is `POST /v1/chats/{chatId}/read` with no body;
- replies use `POST /v1/chats/{chatId}/messages`;
- voice memos use `POST /v1/chats/{chatId}/voicememo`;
- attachments are allocated with JSON, then uploaded by raw `PUT`.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
HERMES_AGENT_SRC=/path/to/hermes-agent .venv/bin/python -m pytest
```

The tests cover exact REST paths and bodies, paginated FULL sync, deterministic
snapshot recovery, WebSocket authentication, commit-before-ACK ordering,
replay deduplication, heartbeat handling, reconnect behavior, and the real
Hermes adapter and plugin registration surfaces.
