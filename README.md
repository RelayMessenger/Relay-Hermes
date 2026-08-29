# hermes-relay-plugin

A Relay platform adapter for
[Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes is an always-on gateway, so this plugin uses Relay's optional WebSocket
transport.

## Reliability

For every WebSocket event, the plugin:

1. commits the complete envelope and `event_id` to a durable SQLite inbox;
2. sends a cumulative ACK only after that commit;
3. processes the inbox after acceptance;
4. deduplicates replayed `event_id` values;
5. replies through `POST /v1/chats/{chatId}/messages`;
6. derives a stable `Idempotency-Key` from the triggering event.

The inbox survives a gateway restart. A processing failure returns the row to
`pending`; it does not move Relay's ACK backward or lose the event.

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

```sh
git clone https://github.com/relaymessenger/hermes-relay-plugin \
  ~/.hermes/plugins/relay
hermes plugins enable relayapp-platform
```

Set the Agent Token:

```sh
RELAY_AGENT_TOKEN=your_agent_token
```

Then start Hermes:

```sh
hermes gateway start
```

On its first connection, the plugin enables:

```http
PUT /v1/websocket
{"enabled":true}
```

It derives `wss://.../v1/websocket` from `RELAY_BASE_URL` and sends the
long-lived Agent Token only in the WebSocket upgrade header:

```http
Authorization: Bearer <Agent Token>
```

The token is never placed in the URL or a cookie. Relay uses no required
WebSocket subprotocol.

Relay reconnects after `heartbeat_timeout`, `restart`, close codes `1011`,
`1012`, or `4408`, and retryable `ack_failed`/`delivery_failed` errors. It
stops on `disabled`, `replaced`, or `revoked` so two consumers do not fight for
one Agent Contact and an operator action is not silently undone. Relay refuses
to disable WebSocket delivery while any event remains unacknowledged.

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

## Current Relay contract

- inbound Message data is the webhook `data` object;
- Chat id is `data.chat.id`;
- sender is `data.sender_handle`;
- text is `part.value`;
- mentions use `part.mention`;
- Read is `POST /v1/chats/{chatId}/read` with no body;
- replies use `POST /v1/chats/{chatId}/messages`;
- voice memos use `POST /v1/chats/{chatId}/voicememo`;
- attachments are allocated with JSON, then uploaded by raw `PUT`.

Hermes' typing callbacks are currently no-ops in this plugin.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
HERMES_AGENT_SRC=/path/to/hermes-agent .venv/bin/python -m pytest
```

The tests cover exact REST paths and bodies, paginated FULL sync, deterministic
snapshot recovery, direct upgrade-header authentication with no query
credential or subprotocol, commit-before-ACK ordering, replay deduplication,
reconnect backoff, and the real Hermes adapter and plugin registration
surfaces.
