# Relay-Hermes follow-up audit evidence (2026-09-01)

This README and `receipt.json` are **derived summaries**, not raw logs. The
authoritative evidence is under `raw/`, and the exact built wheel and sdist are
under `artifacts/`.

All claimed final-source Linux validation ran in fresh private Daytona sandbox
`e4ebee80-2d36-473a-851b-a98d6b59f057`
(`relay-hermes-followup-final-20260901`, Debian 13 x86_64,
`daytona-large`). The sandbox finished in state `archived`.

No hosted Relay environment, production endpoint, deployment, publication,
Relay credential, publication credential, protected workflow secret, or
hosted GitHub Actions run was used.

## Source identity

The tested archive contained the 20 final source files outside `proof/`.
Excluding `proof/` avoids a self-referential receipt while allowing the source
manifest to be reproduced from the final commit.

- Source manifest: `raw/source.sha256`
- Source-manifest SHA-256:
  `255afdf444bc7e96af86b9b64379d657d73581c597ad07a0af3a76ed2d80efae`
- Transferred source archive SHA-256:
  `f6bde9187cb80f9d89357fcc726475b7575ec423f9f54ee80320a6b2c2acd6df`
- Base commit:
  `9babd64a8fbecdb233a021a27c0b4703a675ff86`
- Hermes commit:
  `04224b2f82aabbe89525451089eb2677edfae179`
- Relay Server OpenAPI commit:
  `9b4d5bb32cc749c6fd271969948c385300d404d6`
- OpenAPI SHA-256:
  `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`

`raw/03-setup.*` records environment setup. `raw/08-final-source.*` records
the exact final-source overlay and manifest check. Every claimed test or build
is in `raw/09-*` or later.

## Results derived from raw logs

| Validation | Raw result |
| --- | --- |
| CPython 3.11.14 | `85 passed in 3.19s` |
| CPython 3.12.12 | `85 passed in 3.15s` |
| CPython 3.13.11 | `85 passed in 3.21s` |
| POSIX state-directory mode/symlink/replacement suite | 5 named tests passed |
| Wheel and sdist build | passed for `1.0.0rc1` |
| Clean wheel installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Clean sdist installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Hermes Plugin Doctor | passed on 3.11, 3.12, and 3.13 |
| Hermes clean-wheel runtime | passed on 3.11, 3.12, and 3.13 |
| Locked OpenAPI harness | passed; 5 paths and 9 WebSocket frames |
| Staging helper guards | passed; missing token and production origin returned 2 |
| Workflow static checks | passed; canonical repository, SHA pins, no expressions in `run:` |
| Hosted workflow execution | not run |

The RC workflow itself was not dispatched. Static assertions verify that its
canonical staging preflight gates a reusable exact-SHA three-version CI,
protected contract checkout, protected provenance build, artifact checksums,
and PyPI trusted publishing in dependency order.

## Retained artifacts

- `artifacts/relay_hermes-1.0.0rc1-py3-none-any.whl`
  - SHA-256:
    `f9aaade72ea13bf999bcb8e520c606307c990a207eac912ec91c7a143e9c3dc5`
- `artifacts/relay_hermes-1.0.0rc1.tar.gz`
  - SHA-256:
    `2514c1cce2be390da4a332e521a919378bd6dff5cdf0724f58ae4a747cb96999`

`raw/13-package.command.sh` and `raw/13-package.log` are the complete build,
contents, six clean-install, metadata, and compatibility receipt.

## Evidence integrity

`raw/evidence.sha256` covers every other retained raw command, raw log, JSON
receipt, source manifest, artifact hash list, wheel, and sdist. Its own
SHA-256 is:

`2a089eff704ee467e7356dfa46395d558051c9a720ee5aef93948299cece9abd`

The `*.command.sh` files are the exact shell payloads executed in Daytona using
the repository's SDK command runner; their matching `*.log` files are raw
combined stdout/stderr. Lifecycle `*.command.txt` files are exact local Daytona
CLI commands.
