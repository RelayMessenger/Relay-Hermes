# hermes-relay-plugin

A Relay platform adapter for
[Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes is an always-on gateway, so this plugin uses Relay's optional WebSocket
transport.

## Reliability

For every WebSocket event, the plugin:

1. commits the complete envelope and `event_id` to a FULL-sync SQLite inbox;
2. sends a cumulative ACK only after that commit;
3. processes the inbox after acceptance;
4. deduplicates replayed `event_id` values;
5. replies through `POST /v1/chats/{chatId}/messages`;
6. derives a stable `Idempotency-Key` from the triggering event.

The inbox survives a gateway restart. A processing failure returns the row to
`pending`; it does not move Relay's ACK backward or lose the event.

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

It requests one-use tickets from `POST /v1/websocket-connections` and connects
with the `relay.v1.json` subprotocol.

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

Hermes' typing callbacks are no-ops because Relay v1 has no typing endpoint.

## Development

```sh
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
HERMES_AGENT_SRC=/path/to/hermes-agent .venv/bin/python -m pytest
```

The tests cover exact REST paths and bodies, current event parsing, WebSocket
commit-before-ACK ordering, replay deduplication, durable recovery, and the
real Hermes adapter and plugin registration surfaces.
