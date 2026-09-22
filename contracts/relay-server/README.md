# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `fe3ec1e91608e923ec5ee0e37896eb8bf24d863a` |
| Source commit date | `2026-09-22T01:00:28-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `fe3ec1e91608e923ec5ee0e37896eb8bf24d863a/openapi.yaml` |
| Byte length | `196369` |
| SHA-256 | `262e832ad356375b1a912faa6f9a8ea9c008effa6c24e2618b000ad6b69d858f` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/fe3ec1e91608e923ec5ee0e37896eb8bf24d863a/openapi.yaml
```
