# Relay-Hermes v2 security-correction evidence (2026-09-01)

This is the complete proof closure for the three HIGH corrections applied after
`6f6fc797975a56699473c13394e8412bffd547d6`. Raw commands and logs are under
`raw/`; the only retained release distribution pair is under `artifacts/`.
Implementation choices and this derived summary are agent-made and remain
pending owner and independent-auditor review.

All claimed final-source Linux validation ran in fresh private Daytona sandbox
`69ece5c1-7bd1-4d80-980c-c0d609c99833`
(`relay-hermes-security-v2-20260901`, Debian 13 x86_64, `daytona-large`). Its
final state is `archived`.

No hosted Relay environment, production endpoint, deployment, publication,
Relay credential, publication credential, protected workflow secret, or hosted
GitHub Actions run was used. No post-fix independent audit had been run when
this evidence was written, so the worktree was held back at that point. That
hold is spent: the work was pushed and is on branch `staging` at
`8597004ba2d34a659e64982ad3613179282db54c`.

## Corrected source

The tested archive contains the 21 files from correction commit
`0e8c82e713d127cc943b60d9e6351fe1ea89e075` outside `proof/`. Excluding proof
avoids receipt self-reference; `raw/source.sha256` still lets every tested
source byte be reproduced from that commit.

- Source manifest: `raw/source.sha256`
- Source-manifest SHA-256:
  `4b69f80086089e30a793d6abcfb83c5aeeaa2e77265c015df1bb8c5ee5f73a9e`
- Transferred source archive SHA-256:
  `15d844e4feaf993537da387cdc068667fe422445ce0f44a35316f25abfbad230`
- Audit-input commit:
  `6f6fc797975a56699473c13394e8412bffd547d6`
- Corrected source commit:
  `0e8c82e713d127cc943b60d9e6351fe1ea89e075`
- Hermes commit pinned for this run:
  `04224b2f82aabbe89525451089eb2677edfae179`
  (superseded; CI now pins `b2aa855b626ff8688eb34b95c60ee8b6a4af3679`)
- Relay Server OpenAPI commit for this run:
  `9b4d5bb32cc749c6fd271969948c385300d404d6`
  (superseded; the locked contract is now
  `99906995625ddc00348064a585ada1649313b0fc`)
- OpenAPI SHA-256 for this run:
  `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`
  (superseded; the locked contract now hashes
  `7094178cb01c0ddc05f9254dc91094900a0a7b6273979c0cad6257eec486f0d8`)

## HIGH finding closure

1. **Chat access is not operator authority.** Ordinary admitted Contacts remain
   accepted for normal chat through the profile's chat allowlist. Privileged
   slash commands default deny through Hermes's pinned `allow_admin_from`
   semantics, and `/update` is unavailable to an ordinary Contact. Only an
   explicit profile `RELAY_OPERATOR_CONTACTS`, `operator_contacts`, or native
   profile admin decision grants operator commands. The plugin no longer sets a
   process-global allow-all flag. **Withdrawn the same day:**
   `proof/2026-09-01-v3/README.md` shows this operator-list claim was not valid
   for secondary profiles, and the shipped README states that
   `RELAY_OPERATOR_CONTACTS` and `operator_contacts` are not supported.
2. **All Relay settings are profile-scoped.** Token, API origin, chat allowlist,
   operator decision, state directory, and delivery settings resolve through
   pinned Hermes `agent.secret_scope.get_secret`. A scoped miss under multiplex
   never falls through to process environment. The default inbox is below the
   active Hermes profile home. Multi-profile tests use poisoned process values,
   distinct profile values, a missing-token profile, and two independent SQLite
   inboxes.
3. **SQLite never receives the mutable configured pathname.** State-directory
   components are opened with descriptor-relative `O_DIRECTORY|O_NOFOLLOW`;
   the database is pinned with `openat`, `O_NOFOLLOW`, and regular-file,
   ownership, single-link, and inode checks. Python SQLite connects in
   `mode=rw` through the kernel-owned descriptor path. Tests prove state and
   ancestor symlinks, dangling and existing-target DB symlinks, DB replacement,
   and directory replacement do not redirect creation or mutation into an
   attacker-selected path.

The database schema and public Relay API contract were not changed.

## Results derived from raw logs

| Validation | Raw result |
| --- | --- |
| CPython 3.11.14 | `96 passed in 2.61s` |
| CPython 3.12.12 | `96 passed in 2.60s` |
| CPython 3.13.11 | `96 passed in 2.66s` |
| Dedicated wrapper/profile/operator/state security suite | 17 named tests passed |
| Build backend | `setuptools==84.0.0` |
| Build frontend | `build==1.3.0` (superseded; CI now installs `build==1.6.0`) |
| Retained wheel/sdist distribution build | one pair, passed for `1.0.0rc1` |
| Exact wheel clean installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Exact sdist clean installs | passed with `pip check` on 3.11, 3.12, and 3.13 |
| Plugin Doctor on exact extracted sdist | passed on 3.11, 3.12, and 3.13 |
| Hermes runtime from exact wheel | passed on 3.11, 3.12, and 3.13 |
| Locked OpenAPI and staging helper checks | passed |
| Contract and artifact-lineage workflow checks | passed statically |
| Hosted workflow execution | not run |
| Independent post-fix audit | not run; required before push/publish |

`raw/08-build-once.*` produced the retained distribution pair.
`raw/09-exact-artifacts.*` rehashed and used those exact bytes for six clean
installs, three sdist Plugin Doctor runs, and three wheel Hermes runtime runs.
`raw/10-contract-workflow.*` verifies the locked contract and that publication
rehashes the one CI artifact instead of rebuilding it.

## Retained exact artifacts

- `artifacts/relay_hermes-1.0.0rc1-py3-none-any.whl`
  - SHA-256:
    `76a3213c0ae95fe3a97ccac6924b62c2ad1b419025462b3dc48287d56c0cdbb0`
- `artifacts/relay_hermes-1.0.0rc1.tar.gz`
  - SHA-256:
    `7e946c549ef29bc813200944a296a5da85ce93cd90abcfa4cabc69c703525dd0`

## Mechanically closed evidence

`raw/evidence.sha256` covers every file under `raw/` and `artifacts/` except
itself. Its SHA-256 is
`54f36f541f5574286217139c559af83e91cb262d67199583ae7f21aeaef38263`.
It does not claim to cover this README or `receipt.json`.

`proof-manifest.sha256` separately hashes this README, `receipt.json`, and
`raw/evidence.sha256`. It deliberately does not hash itself, avoiding
self-reference while closing both derived summaries over all retained raw
commands, logs, receipts, manifests, wheel, and sdist.

This README was corrected on 2026-09-08 to mark the superseded pins and the
spent push hold above, and `proof-manifest.sha256` was recomputed for the
corrected text. `raw/`, `artifacts/`, `receipt.json`, and `raw/evidence.sha256`
are unchanged and still record the original run.
