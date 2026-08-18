# hermes-relay-plugin

A **[Relay](https://relayapp.im)** platform adapter for
[Hermes Agent](https://github.com/NousResearch/hermes-agent).

Relay is a messenger where people text AI agents like contacts. This plugin
makes a Relay agent conversation a Hermes channel: your Hermes gets a profile,
a handle, and a thread in someone's message list, and it answers there.

Pure Python. No Node sidecar, no webhook server, no public URL, no signing
secret. Inbound messages arrive by long-polling Relay's durable event log, so
a Hermes running on a laptop behind NAT works exactly like one on a server.

## How it compares to the other Hermes messaging channels

| | Telegram | Photon (iMessage) | **Relay (this plugin)** |
|---|---|---|---|
| Inbound transport | long poll (`getUpdates`) | gRPC stream via a Node sidecar | **long poll (`GET /v1/events`)** |
| Needs a public URL | no | no | **no** |
| Extra runtime deps | `python-telegram-bot` | Node 18+ and `spectrum-ts` | **none beyond Hermes** |
| Agent has its own identity | a bot account | your phone number | **an agent profile people add** |
| Group turns | mention-gated | mention-gated | **invocation-gated by the server** |

The only runtime requirement is `httpx`, which Hermes already ships
(`httpx[socks]==0.28.1` in its own `pyproject.toml`), so installing this
plugin adds nothing to your environment.

## Installation

Copy this directory into your Hermes plugins directory and enable it:

```bash
git clone https://github.com/relaymessenger/hermes-relay-plugin \
  ~/.hermes/plugins/relay
hermes plugins enable relayapp-platform
```

Then put your Agent Token in `~/.hermes/.env`:

```bash
RELAY_AGENT_TOKEN=rly_live_...
```

Or set it through the config UI, where the plugin's env vars appear with
their own prompts and help text:

```bash
hermes config
```

## Getting an Agent Token

1. Open Relay and create an agent (**Create your own**).
2. Copy the Agent Token. Relay shows it **once**, at creation.
3. Paste it into `~/.hermes/.env` as `RELAY_AGENT_TOKEN`.

Full walkthrough: <https://docs.relayapp.im/guides/your-agent>.

Start the gateway and the agent is live:

```bash
hermes gateway start
```

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `RELAY_AGENT_TOKEN` | yes | | Agent Token (`rly_live_...`), shown once at creation |
| `RELAY_BASE_URL` | no | `https://api.relayapp.im` | API origin. HTTPS, unless it is loopback |
| `RELAY_ALLOWED_USERS` | no | | Comma-separated Relay user ids allowed in, besides the owner |
| `RELAY_ALLOW_ALL_USERS` | no | `false` | Let anyone who can reach the agent talk to it |
| `RELAY_STATE_DIR` | no | `~/.hermes/relay` | Where the poll cursor and dedupe window live |
| `RELAY_REPLY_TO_MODE` | no | `auto` | `off`, `first`, `all`, or `auto` |
| `RELAY_HOME_CHANNEL` | no | | Conversation id for cron and notification delivery |
| `RELAY_HOME_CHANNEL_NAME` | no | | Human label for that conversation |

Everything is also settable in `config.yaml`, where env wins over YAML:

```yaml
gateway:
  platforms:
    relayapp:
      enabled: true
      extra:
        token: "rly_live_..."
        base_url: "https://api.relayapp.im"
```

The platform id is `relayapp`, not `relay`. Hermes already ships a built-in
`Platform.RELAY` for its own connector, so this plugin takes the next
available name.

## Security model

**Only the agent's owner is answered, by default.** At connect the adapter
reads `owner_user_id` from `GET /v1/agents/me` and drops every other sender
before the text ever reaches the model. A Hermes with shell and file tools is
not a public endpoint, so opening it up has to be a decision somebody made on
purpose:

```bash
RELAY_ALLOWED_USERS=usr_abc123,usr_def456   # a named few
RELAY_ALLOW_ALL_USERS=true                  # anyone who can reach the agent
```

Relay authenticates senders on the server, so `sender.id` is a real identity
and is safe to authorize on. Nothing user-controlled feeds the check.

Two more properties worth knowing:

* **No inbound secret exists.** Long polling means there is no webhook
  endpoint to expose and no signing secret to leak or rotate. The Agent Token
  is the only credential, it travels outbound only, and it never appears in a
  log line.
* **Attachment URLs are capability URLs.** They are handed to the fetch and
  never logged or reported, because possession of one is possession of the
  file.

## What it does

**Receives.** Text, and images that land in the Hermes image cache as local
paths so the vision tools can read them. Other attachments and voice memos
arrive as an explicit note rather than vanishing.

**Replies.** Plain text, chunked at Relay's 8 KB limit. Markdown a model
emits is stripped, because in a message bubble it reads as literal asterisks,
though code fences survive intact. In a DM each chunk is its own bubble, so a
long answer reads like someone typing rather than one wall of text.

**Stays quiet when there is nothing to say.** A model that must emit something
emits filler. Reply with exactly `[no reply]` and the adapter sends nothing.
This is DM-only: a group invocation is an explicit request for an answer.

**Groups.** A group message carries an `invocation_id` that the reply must
carry back, and the first committed reply completes it, so a group turn goes
out as one message with several ordered parts. Typing signals carry it too.

**Sends media.** Images, documents, video, and native voice memos with an
inline player, uploaded through `POST /v1/attachments`.

**Typing indicators**, on the user's devices while the model works.

**Cron and `hermes send`.** Set `RELAY_HOME_CHANNEL` and `deliver=relayapp`
routes there, including from a process that is not the gateway.

## Delivery guarantees

Relay's event log is at-least-once, and the adapter is built around that:

* The poll **cursor advances only after every event on a page was handled**.
  A handler that fails replays the page instead of skipping past it.
* Events are **deduplicated by `event_id`**, and an id is recorded only after
  its handler succeeded, so a redelivery after a crash is not mistaken for a
  duplicate.
* Cursor and dedupe window are written **together, atomically** (temp file
  plus `os.replace`) under `~/.hermes/relay/`. A restart never reads a
  half-written file.
* Replies carry a **derived `Idempotency-Key`** (`reply-<event_id>-<n>`), so a
  send retried after a timeout commits one message, not two.
* Transient failures back off **500 ms doubling to a 30 s ceiling**, with
  jitter. Terminal failures stop with an explanation instead of hammering.

## Troubleshooting

**"Relay rejected the Agent Token (401)"**. The token is wrong, or it was
rotated. Tokens are shown once at creation; issue a new one in Relay and
update `RELAY_AGENT_TOKEN`.

**"Another consumer took this Agent Token"**. Relay allows one long-poll
consumer per token, and a newer one wins. Something else is running with the
same token, usually a second Hermes or a stray process. Restarting will not
win the slot back. Stop the other consumer, or give this Hermes its own agent
and its own token.

**The agent connects but never answers**. The sender is not the owner, and
no allowlist is set. The log says which. Add `RELAY_ALLOWED_USERS` or
`RELAY_ALLOW_ALL_USERS=true`.

**"cursor expired (410)"**. The cursor fell behind Relay's seven-day event
retention, which means the agent was offline longer than that. Recover the
missed conversation through the history API. Do not reset the cursor to zero:
that replays everything still retained.

**"long poll conflict (409)"**. A webhook endpoint is registered for this
agent. Webhooks and long polling are mutually exclusive per Agent Token.
Remove the webhook to poll.

**Group messages get no reply**. The invocation expired before the model
finished, or a reply already consumed it. One invocation permits exactly one
committed reply.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The transport (`relay_api.py`) and the durable state (`state.py`) carry the
delivery semantics and import nothing from Hermes, so the whole receive path
is tested with a fake HTTP layer and no network. `adapter.py` is only the
binding to `BasePlatformAdapter` and needs a Hermes install to import.

## Provenance

`relay_api.py` is a port of Relay's canonical TypeScript SDK
(`@relaymessenger/sdk`): `client.ts`, `poll-loop.ts`, `idempotency.ts`,
`errors.ts`, `url.ts`, and `memory-dedupe.ts`. The semantics are carried over
deliberately rather than reinvented, so this plugin and Relay's other
integrations fail and recover the same way.

Built and verified against hermes-agent
[`e02d1e41fc6104187e20af9eac8b2820566e3508`](https://github.com/NousResearch/hermes-agent/commit/e02d1e41fc6104187e20af9eac8b2820566e3508)
(2026-08-18).

## License

MIT. Copyright Companion, Inc. See [LICENSE](LICENSE).
