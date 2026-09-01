# Relay-Hermes independent-audit validation (2026-09-01)

All Linux validation ran in fresh private Daytona sandbox
`59631847-53ec-4aac-bd17-b98998c595c5`
(`relay-hermes-audit-final-20260901`, Debian 13 x86_64, `daytona-large`).
No hosted Relay environment, production endpoint, deployment, publication,
Relay credential, publication credential, or GitHub Actions run was used.

## Exact source

The tested archive contains the 19 final-head source files outside `proof/`.
This explicit proof exclusion avoids a self-referential receipt hash.

- Source manifest SHA-256:
  `a1e7f6d1d46fc0d66e4a193fc396dd730ec77afe62b4e826e3b4d037c9fcf865`
- Transferred archive SHA-256:
  `4186ead15aed1bddf9fed548d45bfc97b3194d13e5c49c8df8d74c291870092f`
- Hermes commit:
  `04224b2f82aabbe89525451089eb2677edfae179`
- Relay Server OpenAPI commit:
  `9b4d5bb32cc749c6fd271969948c385300d404d6`
- OpenAPI SHA-256:
  `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`

`logs/source.sha256` is the exact sorted source manifest. The aggregate above
is the SHA-256 of that file and is reproducible from the final head.

## Exact results

| Validation | Result |
| --- | --- |
| CPython 3.11.14 | `80 passed in 1.83s` |
| CPython 3.12.12 | `80 passed in 1.84s` |
| CPython 3.13.11 | `80 passed in 1.81s` |
| Fail-closed URL/account-state selection | `7 passed, 21 deselected in 0.38s` |
| Wheel and sdist build | `relay-hermes==1.0.0rc1`, passed |
| Clean wheel installs | Python 3.11, 3.12, and 3.13 passed `uv pip check` |
| Plugin manifest | `1.0.0-rc.1` |
| Hermes Plugin Doctor | passed |
| Hermes directory and wheel harnesses | passed |
| Staging helper | passed; missing token and production origin both returned 2 |
| Locked OpenAPI assertions | passed; 5 paths and 9 WebSocket frames |
| Workflow assertions | all external actions SHA-pinned; RC workflow manual, exact-SHA, staging-only, OIDC, and provenance guarded |

Artifacts built from the exact source:

- `relay_hermes-1.0.0rc1-py3-none-any.whl`:
  `a509868a334878f0a6bf8fa79f862739a8283646d487f5b79cea76cdbfbac51e`
- `relay_hermes-1.0.0rc1.tar.gz`:
  `cab6520fbfaaf1ce17a1747685e9656032a2b74a72db1d5a26bb4610cb700713`

The suite mocked Relay network I/O. The hosted CI and guarded PyPI workflow
were inspected and exercised by static tests only; neither workflow was run.
See `logs/commands.txt`, `logs/results.log`, and `receipt.json`.
