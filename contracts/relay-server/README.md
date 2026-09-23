# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `51bc3ecd9b203a3fc75fe0ab7a105b6751080678` |
| Source commit date | `2026-09-23T16:10:05-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `51bc3ecd9b203a3fc75fe0ab7a105b6751080678/openapi.yaml` |
| Byte length | `229792` |
| SHA-256 | `7b41c21bebd99d28d103da1c3fe380642542e5b6243bb4319e501d7609d8ab0f` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/51bc3ecd9b203a3fc75fe0ab7a105b6751080678/openapi.yaml
```
