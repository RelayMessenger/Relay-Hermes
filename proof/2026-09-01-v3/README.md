# Relay-Hermes v3 slash-isolation evidence (2026-09-01)

This proof supersedes `proof/2026-09-01-v2/`. The v2 claim that an explicit
profile Relay operator list could safely grant privileged slash commands was
not valid for secondary profiles because pinned Hermes slash policy ignores
`source.profile`. The historical v2 files remain unchanged, but must not be
used as release evidence. This v3 closure is authoritative for corrected source
commit `bb7185916e65c6af2cf319993a560024e8c649df`.

Raw commands and logs are under `raw/`; the only retained release distribution
pair is under `artifacts/`. Implementation choices and this derived summary are
agent-made and remain pending owner and independent-auditor review.

All claimed final-source Linux validation ran in fresh private Daytona sandbox
`edd173f2-fb53-48c4-b5b1-c3e42fe6e7a5`
(`relay-hermes-slash-v3-20260901`, Debian 13 x86_64, `daytona-large`). Its final
state is `archived`.

No hosted Relay environment, production endpoint, deployment, publication,
Relay credential, publication credential, protected workflow secret, or hosted
GitHub Actions run was used. No post-fix independent audit had been run when
this evidence was written, so the worktree was held back at that point. That
hold is spent: the work was pushed and is on branch `staging` at
`8597004ba2d34a659e64982ad3613179282db54c`.

## Exact corrected source

The tested archive contains the 21 files from commit
`bb7185916e65c6af2cf319993a560024e8c649df` outside `proof/`.

- Source manifest: `raw/source.sha256`
- Source-manifest SHA-256:
  `b965853938713daf884768028db9abc283e8bbec6ecad56b1e2a2841be702aad`
- Transferred source archive SHA-256:
  `756eb83968b20f8f49a853b4f9fd477d5e9fa0ba5e5cb1bdd2982d62ba0b5fd7`
- Superseded v2 closure commit:
  `b3ca426a9e820e967203159e50797ef2d84992a7`
- Corrected source commit:
  `bb7185916e65c6af2cf319993a560024e8c649df`
- Hermes commit pinned for this run:
  `04224b2f82aabbe89525451089eb2677edfae179`
  (superseded; CI now pins `b2aa855b626ff8688eb34b95c60ee8b6a4af3679`)
- Relay Server OpenAPI commit for this run:
  `9b4d5bb32cc749c6fd271969948c385300d404d6`
  (superseded; `contracts/relay-server/README.md` names the pinned
  commit today)
- OpenAPI SHA-256 for this run:
  `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`
  (superseded; `contracts/relay-server/README.md` gives its SHA-256
  today)

## Remaining HIGH closure

Pinned Hermes `gateway.slash_access.policy_for_source` reads the runner's
primary `PlatformConfig` and does not select policy with `source.profile`.
Relay-Hermes does not patch or mutate Hermes core and does not attempt a global
profile-policy bridge.

The plugin instead fails closed independently of profile:

1. every inbound Relay message whose first non-whitespace character is `/` is
   withheld before Hermes `MessageEvent` dispatch;
2. every attempted legacy or Hermes-native Relay slash grant is replaced with
   deny-only admin and user command lists as defense in depth;
3. `relayapp` registers `allow_update_command=False`;
4. `RELAY_OPERATOR_CONTACTS` is removed from the manifest and
   `operator_contacts` is retired and ignored; and
5. ordinary allowed Contact messages continue to dispatch normally.

The exact pinned-Hermes multi-profile test constructs primary and secondary
Relay adapters, attempts primary and secondary `/update` grants, gives each
source a distinct `source.profile`, confirms pinned `policy_for_source` denies
both, confirms neither `/update` reaches adapter dispatch, and confirms ordinary
chat from both profiles still dispatches.

The database schema and public Relay API contract were not changed. The prior
profile credential/state and descriptor/no-follow SQLite corrections remain in
place and are included in the full and dedicated security suites.

## Results derived from raw logs

| Validation | Raw result |
| --- | --- |
| CPython 3.11.14 | `97 passed in 2.74s` |
| CPython 3.12.12 | `97 passed in 2.77s` |
| CPython 3.13.11 | `97 passed in 2.81s` |
| Dedicated wrapper/slash/profile/state security suite | 18 named tests passed |
| Exact pinned-Hermes primary/secondary `/update` test | passed |
| Build backend | `setuptools==84.0.0` |
| Build frontend | `build==1.3.0` (superseded; CI now installs `build==1.6.0`) |
| Retained wheel/sdist distribution build | one pair, passed for `1.0.0rc1` |
| Exact wheel clean installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Exact sdist clean installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Plugin Doctor on exact extracted sdist | passed on 3.11, 3.12, and 3.13 |
| Hermes runtime from exact wheel | passed with secondary `/update` denied on all three |
| Locked OpenAPI and staging helper checks | passed |
| Contract and artifact-lineage workflow checks | passed statically |
| Hosted workflow execution | not run |
| Independent post-fix audit | not run; required before push/publish |

`raw/08-build-once.*` produced the retained distribution pair.
`raw/09-exact-artifacts.*` rehashed and used those exact bytes for six clean
installs, three sdist Plugin Doctor runs, and three pinned-Hermes wheel runtime
runs. `raw/10-contract-workflow.*` verifies the locked contract, slash static
invariants, and one-artifact publication lineage.

## Retained exact artifacts

- `artifacts/relay_hermes-1.0.0rc1-py3-none-any.whl`
  - SHA-256:
    `013da952474f36579a247575cfe57571fff53d23d6fd67490c3805a34f1fb08b`
- `artifacts/relay_hermes-1.0.0rc1.tar.gz`
  - SHA-256:
    `ee73111b5dbdf1ac93101a3debb7ac584b6bec6418e916e2f91dfa0ef81aa9d6`

## Mechanically closed evidence

`raw/evidence.sha256` covers every file under `raw/` and `artifacts/` except
itself. Its SHA-256 is
`480cc2c64a3e290c696850968c3f13bee3db38fb97637d4b3b1377f3a8148053`.
It does not claim to cover this README or `receipt.json`.

`proof-manifest.sha256` separately hashes this README, `receipt.json`, and
`raw/evidence.sha256`. It deliberately does not hash itself, avoiding
self-reference while closing both summaries over all retained evidence.

This README was corrected on 2026-09-08 to mark the superseded pins and the
spent push hold above, and `proof-manifest.sha256` was recomputed for the
corrected text. `raw/`, `artifacts/`, `receipt.json`, and `raw/evidence.sha256`
are unchanged and still record the original run.
