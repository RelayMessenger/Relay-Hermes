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
5. explicitly marks the Chat Read at intake, as soon as the message is
   accepted for Hermes (before Hermes decides whether it starts a new turn
   or folds the message into one already running);
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

Create the Agent with the
[Relay CLI](https://www.npmjs.com/package/relaymessenger). It gives the Agent a `.dev` handle, saves the Agent Token in your Relay
profile, and writes `RELAY_AGENT_TOKEN`, `RELAY_BASE_URL`, and
`RELAY_STATE_DIR` into `~/.hermes/.env`:

```sh
npx relaymessenger agents create \
  --connect hermes \
  --runtime-home "$HOME/.hermes" \
  --runtime-state-dir "$HOME/.hermes/relay" \
  --runtime-stopped --confirm-configure
```

Stop Hermes first; `~/.hermes/config.yaml` must already exist. The CLI writes
that one file and never starts Hermes. An Agent is created only this way.

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

### Display defaults

Hermes keeps a built-in table of per-platform display defaults
(`gateway/display_config.py`); its iMessage adapters (`bluebubbles`,
`photon`) sit in the quiet tier there, with tool progress, interim
commentary, heartbeats, and streaming previews off, because those inboxes
cannot edit a message once sent. A plugin cannot add a row to that table, so
`relayapp` inherits the global defaults, which were written for platforms
that edit in place.

The adapter itself refuses tool-progress lines (`format_tool_event` returns
`None`). Everything else is a user setting. To read like iMessage, set these
under `display.platforms.relayapp` in `~/.hermes/config.yaml` (key names from
`hermes_cli/config_defaults.py` and `gateway/display_config.py`; an unset key
falls through to the global `display.<key>`):

```yaml
display:
  platforms:
    relayapp:
      tool_progress: off
      interim_assistant_messages: false
      long_running_notifications: false
      streaming: false
      busy_ack_detail: false
      tool_preview_length: 0
```

`display.busy_input_mode` (default `interrupt`) is global, not per platform,
and is left alone: a message that arrives while Hermes is mid-turn is folded
into that turn, queued behind it, or (in `queue` mode) buffered for 0.35 s
and then queued. The adapter marks it Read at intake and leaves the reply
unquoted, so it reads as it does on iMessage. Its durable inbox row is
settled only when the session goes quiet: at the successful end of the turn
that consumed it, unless Hermes still holds it for a later turn, in which
case that later turn settles it. If the gateway dies before then the row
replays on restart, which is the safe side.

Hermes core posts a busy acknowledgement bubble when a message lands
mid-turn ("Redirected current run", "Interrupting current task"); its iMessage
adapters show the same bubble, and this plugin does not suppress it. Hermes's
own environment variable turns it off for every platform
(`gateway/run_busy.py`: `os.environ.get("HERMES_GATEWAY_BUSY_ACK_ENABLED",
"true").lower() != "true"` suppresses the ack):

```sh
export HERMES_GATEWAY_BUSY_ACK_ENABLED=false
```

Relay resolves the token, API origin, chat allowlist, state directory, and
delivery settings through Hermes's shared adapter credential reader. The primary
profile uses its own process environment during unscoped startup. A secondary
profile's installed secret scope remains authoritative: a missing value never
falls through to the primary profile's process environment. The default state path is under the active profile home,
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
`1a2245dd775f781b57e0d1f6f3146ebd384c90c3`. The exact
`contracts/developer/openapi.yaml` SHA-256 is
`5458497fe8db4ee7dfe6bef67f2803137575d3ea4d835748290a5c9f8d906791`.
Those exact public bytes are checked in at
`contracts/relay-server/1a2245dd775f781b57e0d1f6f3146ebd384c90c3/openapi.yaml`;
normal CI and RC publication validate that local snapshot without private
repository credentials.

The runtime contract used here is:

- `GET /v1/websocket` for acknowledged agent events;
- `GET /v1/chats` and `GET /v1/chats/{chatId}/messages` only for a
  server-directed FULL sync;
- `POST /v1/chats/{chatId}/read` with no request body at intake;
- `POST /v1/attachments`, followed by the allocated raw `PUT`, for local
  files;
- `POST /v1/chats/{chatId}/messages` with `Idempotency-Key` for every Message
  send;
- event envelopes with `api_version: v1` and
  `webhook_version: 2026-08-30`.

Relay vocabulary in the adapter is Contact, Handle, Chat, Message, and
Participant.

## Releasing

Nothing is published by hand. There are two lanes, the same two Relay-SDK
uses for npm, in PEP 440:

1. Every merge to `staging` publishes a development build. The
   `Publish staging to PyPI` workflow decides the next version from what PyPI
   holds, commits `release: version the staging package automatically` to
   `staging` (it rewrites `pyproject.toml` and `plugin.yaml`, nothing else),
   runs the full CI at that commit, and uploads the wheel and sdist. The
   version is the target with a `.devN` suffix: `1.0.0rc3.dev0`,
   `1.0.0rc3.dev1`, and so on while the target is the candidate `1.0.0rc3`;
   `1.0.0.dev0` once the target is `1.0.0`.
2. A promotion is a pull request from `staging` to `main`, merged without a
   merge commit, so `main` is always a commit `staging` already has. The
   `Release to PyPI` workflow on `main` strips `.devN`, publishes the target
   (`1.0.0rc3`, later `1.0.0`), reads it back from PyPI, and records the tag
   `v1.0.0rc3`. A target PyPI already has is skipped, so a second promotion
   of the same tree publishes nothing.

`pip install relay-hermes` never selects a `.dev` release. `pip install --pre
relay-hermes` does, and `pip install relay-hermes==1.0.0rc3.dev0` names one
exactly. `plugin.yaml` carries the same version in its manifest form:
`1.0.0-rc.3.dev.0` for `1.0.0rc3.dev0`, `1.0.0-rc.3` for `1.0.0rc3`,
`1.0.0-dev.0` for `1.0.0.dev0`.

To aim at a different target, edit the version in `pyproject.toml` (and the
manifest form in `plugin.yaml`) in a normal pull request to `staging`:
`1.0.0` or `1.0.0.dev0` ends the candidate line and starts the GA line,
`1.1.0` starts a minor. The staging lane keeps a hand-written unpublished
version as written and carries on from there. The rules are the table at the
top of `scripts/release_version.py`, and every pull request rehearses both
lanes against live PyPI without publishing (`Rehearse the staging bump` and
`Rehearse the release` in CI).

### Credentials

Both lanes publish through one step, `.github/actions/publish-pypi`. Which
credential it uses is the repository variable `PYPI_TRUSTED_PUBLISHING`:

- unset (today): the project-scoped `PYPI_API_TOKEN` secret, attestations
  off (PEP 740 attestations only work through Trusted Publishing);
- `true`: PyPI Trusted Publishing over OIDC, no token, attestations on.

To switch, register two trusted publishers on the `relay-hermes` project
(PyPI: project settings, Publishing, GitHub), one per lane. Every field is
exact:

| Field | Staging lane | Release lane |
| --- | --- | --- |
| Owner | `RelayMessenger` | `RelayMessenger` |
| Repository name | `Relay-Hermes` | `Relay-Hermes` |
| Workflow name | `publish-staging.yml` | `release.yml` |
| Environment name | `pypi-staging` | `pypi-release` |

Then set the repository variable `PYPI_TRUSTED_PUBLISHING` to `true`
(GitHub: Settings, Secrets and variables, Actions, Variables). No file
changes. The two workflow names are the calling workflows on purpose: PyPI
cannot match a reusable workflow, so the shared step is a composite action
inside those jobs. The `pypi-staging` and `pypi-release` environments are
where any approval rule goes; the older `pypi-rc` environment belongs to the
manual `Publish release candidate` workflow, which stays until the two lanes
have each published once and is then removed.

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
