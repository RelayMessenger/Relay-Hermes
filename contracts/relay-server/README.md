# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `eb83978b6b2c625da82471e4af16acad8de0e618` |
| Source commit date | `2026-09-19T17:44:20-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `eb83978b6b2c625da82471e4af16acad8de0e618/openapi.yaml` |
| Byte length | `165915` |
| SHA-256 | `27698655d12500fb9cd2e10dbf1c94025fbc64c288df6151db673a7649877111` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/eb83978b6b2c625da82471e4af16acad8de0e618/openapi.yaml
```
