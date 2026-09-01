# Relay-Hermes validation proof (2026-09-01)

All Linux execution in this proof ran in one fresh Daytona sandbox. No local
Linux VM, production system, hosted Relay environment, deployment, publish, or
secret was used.

## Provenance

- Worktree base: `c1cec22007e1b2cc27ce2aa7cf671af2a7840db2`
  (`origin/staging` at worktree creation)
- Dedicated branch: `codex/relay-hermes-20260901`
- Final tested source archive SHA-256:
  `43a0bfbbc8936ce3b8447d59ec2d5d90e31b186e42fc7c17ef93a1403346e9c7`
- Hermes source: `04224b2f82aabbe89525451089eb2677edfae179`
  (the `NousResearch/hermes-agent` main HEAD audited on 2026-09-01)
- Relay Server OpenAPI commit:
  `9b4d5bb32cc749c6fd271969948c385300d404d6`
- `contracts/developer/openapi.yaml` SHA-256:
  `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5`
- Daytona sandbox: `7f66c025-9a3e-4010-873d-f9509e38ae02`
  (`relay-hermes-validation-20260901`, Debian 13 x86_64,
  `daytonaio/sandbox:0.8.0`)
- Sandbox final desired state: `archived`

## Final results

| Validation | Exact runtime | Result | Receipt |
| --- | --- | --- | --- |
| Pytest | CPython 3.11.14 | 69 passed | `logs/11a-final-py311-tests.*` |
| Pytest | CPython 3.12.12 | 69 passed | `logs/12a-final-py312-tests.*` |
| Pytest | CPython 3.13.11 | 69 passed | `logs/13a-final-py313-tests.*` |
| sdist and wheel build | CPython 3.13.11 | passed | `logs/14-final-package-harness.*` |
| Clean wheel install and `uv pip check` | CPython 3.13.11 | passed | `logs/14-final-package-harness.*` |
| Wheel `hermes_agent.plugins` registration | Hermes `04224b2` | passed | `logs/14-final-package-harness.*` |
| Directory-plugin runtime discovery | Hermes `04224b2` | passed in each pytest matrix run | `logs/11a-*` through `logs/13a-*` |
| Pip-entrypoint runtime discovery | Hermes `04224b2` | passed in each pytest matrix run | `logs/11a-*` through `logs/13a-*` |
| Hermes Plugin Doctor | Hermes `04224b2` | passed | `logs/15-plugin-doctor.*` |
| Staging helper safety/syntax | Debian `sh` | passed | `logs/16-staging-helper-harness.*` |
| Locked OpenAPI assertions | Python 3.13.11 + PyYAML 6.0.3 | passed | `logs/17b-openapi-contract-harness.*` |

Package artifacts built in Daytona:

- `relay_hermes-1.0.0-py3-none-any.whl`:
  `cede276816c24b8db09c8357a17cf93806fe35d9fdd1c6d6bad0e402ff5047f6`
- `relay_hermes-1.0.0.tar.gz`:
  `b035a12c5bfb281ce64e4fc9be3edca0d23168f608e1680ed46ee56f3703ea1b`

The package harness also verified that `plugin.yaml` is present in the wheel,
and that `plugin.yaml`, `scripts/run-staging.sh`, and `tests/conftest.py` are
present in the source distribution.

## Scope of validation

The suite uses mocks for Relay network I/O and the exact locked OpenAPI for
contract assertions. It does not connect to staging or production. The tests
cover acknowledged WebSocket ordering and recovery, durable deduplication,
Read timing, idempotent Message sends, the absence of Message-effect routes,
current Relay vocabulary/configuration, package metadata, and both Hermes
plugin-loading paths.

Every `.command.txt` file contains the exact command for its adjacent log.
