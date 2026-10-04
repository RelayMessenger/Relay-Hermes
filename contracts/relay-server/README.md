# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `78e958bd35f5e7f33c1ce9b77ac11be1dac3afc8` |
| Source commit date | `2026-10-04T16:45:36-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `openapi.yaml` |
| Byte length | `361290` |
| SHA-256 | `abe76bc8feadd85462ff4293eba9bc1b2cea44b9929b0fbc772a120d84efb365` |

The snapshot was copied byte-for-byte from the locked source. It sits at a
short path on purpose: Hermes copies the whole plugin into a dependency
workspace several directories deep, and on Windows a path over 260
characters fails with WinError 206. The source commit is recorded above, not
in the path. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/openapi.yaml
```
