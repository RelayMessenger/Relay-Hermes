# Relay-Hermes v4 local-contract release evidence (2026-09-01)

This proof supplements `proof/2026-09-01-v3/` and closes only the RC
publication prerequisite changed after audited public staging commit
`6e2f06d7da73c1430a7100485b279eae565c37a9`. The v3 evidence remains the
source/runtime security proof. This v4 evidence targets release-enablement
source commit `25d35866b84efeeaf8c9858db148ca16553aded4`.

Raw commands and logs are under `raw/`; the only retained release distribution
pair is under `artifacts/`. Implementation and evidence-summary choices are
agent-made and remain pending review.

All claimed Linux validation ran in fresh private Daytona sandbox
`4443c705-f35c-4172-839e-1520e2c64d52`
(`relay-hermes-contract-v4-20260901`, Debian 13 x86_64, `daytona-large`). Its
final state is `archived`.

No hosted Relay environment, production endpoint, deployment, publication,
Relay credential, publication credential, protected workflow secret, hosted
GitHub Actions run, push, or release was used.

## Exact release-enablement source

The tested archive contains the 23 files from source commit
`25d35866b84efeeaf8c9858db148ca16553aded4` outside `proof/`.

- Audited public staging base:
  `6e2f06d7da73c1430a7100485b279eae565c37a9`
- Release-enablement source:
  `25d35866b84efeeaf8c9858db148ca16553aded4`
- Source manifest: `raw/source.sha256`
- Source-manifest SHA-256:
  `26d892a3105044e1c4f2469d41759a8e3a90a3e3db7fcd9935365a156c4617aa`
- Transferred source archive SHA-256:
  `f16a1275235a496ff8bf945c5868dbf058fd56deb80d437d25dc9644be3a0d22`
- Pinned Hermes commit:
  `04224b2f82aabbe89525451089eb2677edfae179`

The exact source diff is limited to the two workflows, workflow tests, contract
metadata, the locked contract snapshot, and README contract documentation.
Runtime modules, `plugin.yaml`, `pyproject.toml`, packaging configuration,
staging helper, and contract harness are unchanged from the audited base.

## Locked public OpenAPI

The checked-in snapshot is
`contracts/relay-server/9b4d5bb32cc749c6fd271969948c385300d404d6/openapi.yaml`.

- Source repository: `RelayMessenger/Relay-Server`
- Source commit: `9b4d5bb32cc749c6fd271969948c385300d404d6`
- Source path: `contracts/developer/openapi.yaml`
- Byte length: `117289`
- SHA-256:
  `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`

`raw/02-source.log` proves the checked-in file is byte-identical to workspace
input `_runtime/relay-openapi-locked-9b4d5bb.yaml`.
`raw/04-contract-workflow.log` proves the harness accepts those exact bytes,
normal CI validates the local path before building, and the RC contract job
validates the same local path without a Relay-Server checkout or
`RELAY_CONTRACT_READ_TOKEN`.

## Results derived from raw logs

| Validation | Raw result |
| --- | --- |
| Updated contract/workflow tests | `16 passed in 0.06s` |
| Full CPython 3.13.11 suite | `98 passed in 2.81s` |
| Locked OpenAPI harness | passed with exact commit and SHA-256 |
| Workflow YAML and immutable action pins | passed statically for both workflows |
| Private Relay-Server checkout/token prerequisite | absent |
| CI/publish local snapshot path | exact and shared |
| Distribution build | one retained wheel/sdist pair for `1.0.0rc1` |
| Wheel and sdist clean installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Installed runtime payload | exact source bytes on all six clean installs |
| Plugin Doctor on exact sdist | passed on 3.13 |
| Hermes load from exact wheel | passed on 3.13 |
| Publish workflow rebuild | absent |
| Provenance/publish artifact rehash | retained |
| Hosted workflow execution | not run |

The source contract snapshot is intentionally not package runtime data and is
not included in the wheel or sdist. The package version and runtime payload are
unchanged. `raw/06-build-once.*` produced one release distribution pair, and
`raw/07-exact-packages.*` used those same bytes for every package check.

## Retained exact artifacts

- `artifacts/relay_hermes-1.0.0rc1-py3-none-any.whl`
  - SHA-256:
    `d0aaa8aba4f692a1ba363dd35fce08bcba06a4e07c526608c5488722456a6d3e`
- `artifacts/relay_hermes-1.0.0rc1.tar.gz`
  - SHA-256:
    `5b378167b155632b964277f9a05eb0162391678875109f20407b47342ced4457`

## Mechanically closed evidence

`raw/evidence.sha256` covers every file under `raw/` and `artifacts/` except
itself. Its SHA-256 is
`4d18b03771c8bb31f9ce4f9fee4bcb7a7f3950da122a34c43c8b20253f701dc7`.
It includes the initial archive request conflict while the sandbox was still
stopping, the successful retry, intermediate archiving states, and the final
archived state.

`proof-manifest.sha256` separately hashes this README, `receipt.json`, and
`raw/evidence.sha256`. It deliberately does not hash itself, avoiding
self-reference while closing both summaries over all retained evidence.
