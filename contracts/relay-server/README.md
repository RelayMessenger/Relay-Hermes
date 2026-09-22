# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `b334eba06ce194cee4ee1b6d308145789d90a6fd` |
| Source commit date | `2026-09-22T01:00:28-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `b334eba06ce194cee4ee1b6d308145789d90a6fd/openapi.yaml` |
| Byte length | `202823` |
| SHA-256 | `a64a98ca91ad7298b5e2584032453034bbeba925a5fe20f7808944e62404a9cf` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/b334eba06ce194cee4ee1b6d308145789d90a6fd/openapi.yaml
```
