# Relay-Hermes final audit-correction evidence (2026-09-01)

This README and `receipt.json` are derived summaries. Raw commands and logs are
under `raw/`; the one wheel/sdist pair built before all artifact validation is
retained under `artifacts/`.

All claimed final-source Linux validation ran in fresh private Daytona sandbox
`d64bc9d6-8cae-4e86-9fa2-2ca454f34fd5`
(`relay-hermes-final-corrections-20260901`, Debian 13 x86_64,
`daytona-large`). Its final state is `archived`.

No hosted Relay environment, production endpoint, deployment, publication,
Relay credential, publication credential, protected workflow secret, or
hosted GitHub Actions run was used.

## Exact source

The tested archive contained the 21 final source files outside `proof/`.
Excluding `proof/` avoids source-receipt self-reference while allowing the
manifest to be reproduced from the final commit.

- Source manifest: `raw/source.sha256`
- Source-manifest SHA-256:
  `a4107525372c4fccef190be5185bea91ab86972e39f7af5a479bd78e28e69306`
- Transferred source archive SHA-256:
  `f21b327721124287517290ab9b7d2d8f6921d6152d60680dff1a03fd36997ece`
- Base commit:
  `cf99be34efb0b5527a6a9c8838423b462d282c34`
- Hermes commit tested here:
  `04224b2f82aabbe89525451089eb2677edfae179`
  (superseded; CI now pins `b2aa855b626ff8688eb34b95c60ee8b6a4af3679`)
- Relay Server OpenAPI commit tested here:
  `9b4d5bb32cc749c6fd271969948c385300d404d6`
  (superseded; the locked contract is now
  `99906995625ddc00348064a585ada1649313b0fc`)
- OpenAPI SHA-256 tested here:
  `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`
  (superseded; the locked contract now hashes
  `7094178cb01c0ddc05f9254dc91094900a0a7b6273979c0cad6257eec486f0d8`)

## Results derived from raw logs

| Validation | Raw result |
| --- | --- |
| CPython 3.11.14 | `88 passed in 2.81s` |
| CPython 3.12.12 | `88 passed in 2.93s` |
| CPython 3.13.11 | `88 passed in 2.90s` |
| Wrapper delegation plus state no-follow/replacement suite | 8 named tests passed |
| Build backend | `setuptools==84.0.0` |
| Build frontend | `build==1.3.0` (superseded; CI now installs `build==1.6.0`) |
| One wheel/sdist build | passed for `1.0.0rc1` |
| Exact wheel clean installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Exact sdist clean installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Plugin Doctor on exact extracted sdist | passed on 3.11, 3.12, and 3.13 |
| Hermes runtime from exact wheel | passed on 3.11, 3.12, and 3.13 |
| Locked OpenAPI/helper checks | passed |
| Artifact-lineage workflow checks | one build; exact download, rehash, attest, and publish path |
| Hosted workflow execution | not run |

`raw/07-build-once.*` built the only distribution pair.
`raw/08-exact-artifacts.*` then used those same bytes for all six clean
installs, three sdist Plugin Doctor runs, and three clean-wheel Hermes runtime
runs. `raw/10-contract-workflow.*` verifies that publication contains no build
step and downloads and rehashes the same retained CI artifact.

## Retained exact artifacts

- `artifacts/relay_hermes-1.0.0rc1-py3-none-any.whl`
  - SHA-256:
    `6d868840cb31848c38684da3f06866c97d48e52ad49e7ff25df3ce38bb45a86a`
- `artifacts/relay_hermes-1.0.0rc1.tar.gz`
  - SHA-256:
    `bb51b743e64199ab8a86eeb3f43b184b9906bb362157fef2fc5245d228823f3e`

## Mechanically closed evidence

`raw/evidence.sha256` covers `raw/**` and `artifacts/**`, excluding only itself.
It does **not** claim to cover this README or `receipt.json`.

`proof-manifest.sha256` is the separate top-level closure manifest. It hashes:

- this README;
- `receipt.json`; and
- `raw/evidence.sha256`, which transitively closes every raw command, log,
  JSON sandbox receipt, source manifest, artifact hash list, wheel, and sdist.

`proof-manifest.sha256` deliberately does not hash itself, so there is no
self-reference.

This README was corrected on 2026-09-08 to mark the superseded pins above, and
`proof-manifest.sha256` was recomputed for the corrected text. `raw/`,
`artifacts/`, `receipt.json`, and `raw/evidence.sha256` are unchanged and still
record the original run.
