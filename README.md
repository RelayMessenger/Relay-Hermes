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
It also does not add reactions, edits, or typing indicators.

### Recovery

The SQLite inbox survives gateway restarts. If processing fails, the event
returns to `pending`; the durable transport checkpoint does not move backward.

The state directory and SQLite database are bound to one Relay account using
the normalized API origin and a one-way Agent Token fingerprint. The token is
never written to state. Changing the token or API origin while reusing a state
directory fails before startup requeues work or reads a FULL-sync snapshot.
On supported Linux systems every directory component and the database are
opened through pinned descriptors with no-follow checks before SQLite receives
an already-open file descriptor path. SQLite never connects to the mutable
configured pathname and cannot create through a dangling database symlink or a
replaced directory. The directory is forced to mode `0700`, including when it
already exists. Use a separate `RELAY_STATE_DIR` for every staging or
production Agent.
Pre-binding databases are not adopted automatically; move one aside only after
accounting for its pending work.

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
| `RELAY_STATE_DIR` | `<Hermes profile home>/relay` | Profile-scoped durable SQLite inbox directory |
| `RELAY_REPLY_TO_MODE` | `auto` | Reply anchor policy: `off`, `first`, `all`, or `auto` |
| `RELAY_GROUP_CHAT_POLICY` | `mentions` | Group Chat policy: `mentions` or `all` |
| `RELAY_HOME_CHAT` | unset | Chat id for cron and direct `hermes send` delivery |
| `RELAY_HOME_CHAT_NAME` | Chat id | Human label for the home Chat |

`RELAY_BASE_URL` must be an HTTPS origin, except that HTTP is accepted for
loopback development. A configured invalid value fails closed and is never
replaced with the production default.

### Slash-command limitation

Relay Contact chat is enabled, but Relay slash commands are disabled on the
pinned Hermes core. Its slash-policy resolver reads the gateway runner's
primary platform config and ignores `source.profile`, so a primary profile
operator grant could otherwise authorize a secondary-profile Contact. The
adapter therefore withholds every Relay message whose first non-whitespace
character is `/` from Hermes, installs deny-only slash policy as defense in
depth, and registers `relayapp` as ineligible for `/update`. This is consistent
for primary and secondary profiles; use a trusted local CLI or authenticated
dashboard for operator commands.

`RELAY_OPERATOR_CONTACTS` and `operator_contacts` are not supported. Any old
setting should be removed; it cannot safely grant Relay slash authority until
the pinned Hermes policy becomes profile-aware.

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

Relay resolves the token, API origin, chat allowlist, state directory, and
delivery settings through Hermes's active profile secret scope. In a
multiplexed gateway, a missing value never falls through to another profile's
process environment. The default state path is under the active profile home,
so profile inboxes are distinct even when neither profile sets
`RELAY_STATE_DIR`.

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
`99906995625ddc00348064a585ada1649313b0fc`. The exact
`contracts/developer/openapi.yaml` SHA-256 is
`7094178cb01c0ddc05f9254dc91094900a0a7b6273979c0cad6257eec486f0d8`.
Those exact public bytes are checked in at
`contracts/relay-server/99906995625ddc00348064a585ada1649313b0fc/openapi.yaml`;
normal CI and RC publication validate that local snapshot without private
repository credentials.

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
