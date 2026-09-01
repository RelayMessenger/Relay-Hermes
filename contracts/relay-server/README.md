# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `9b4d5bb32cc749c6fd271969948c385300d404d6` |
| Source commit date | `2026-08-31T23:04:39-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `9b4d5bb32cc749c6fd271969948c385300d404d6/openapi.yaml` |
| Byte length | `117289` |
| SHA-256 | `f62f431fc0daa48500926bf87753f81c3fdda25ab463b130ca97f2896367e0a5` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/9b4d5bb32cc749c6fd271969948c385300d404d6/openapi.yaml
```
