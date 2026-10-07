# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `6f50fcb69d1ce6dde8bf0fb0e12bd2ef379f0b09` |
| Source commit date | `2026-10-04T23:05:08-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `openapi.yaml` |
| Byte length | `362330` |
| SHA-256 | `79bd85b0150ef45ea4bbe5f498507dd86d784e7fbf6c3b299a5a81db091aacdd` |

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
