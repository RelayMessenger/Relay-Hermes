# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `55e12f23fdb559e23e54e4f386a77fd397293834` |
| Source commit date | `2026-09-17T15:46:14-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `55e12f23fdb559e23e54e4f386a77fd397293834/openapi.yaml` |
| Byte length | `148197` |
| SHA-256 | `de33237b05b09414c1994446f746795ab8bf410cb2c8f1422259103775cfc182` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/55e12f23fdb559e23e54e4f386a77fd397293834/openapi.yaml
```
