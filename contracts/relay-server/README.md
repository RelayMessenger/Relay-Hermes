# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `e53138b79536f2fb8bbd339e6d344819c0afe8ff` |
| Source commit date | `2026-09-26T06:35:56-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `e53138b79536f2fb8bbd339e6d344819c0afe8ff/openapi.yaml` |
| Byte length | `325987` |
| SHA-256 | `3ac33f08a16f83be44585a34df34d7067f9157a8971e63686ab41f44374ce5f8` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/e53138b79536f2fb8bbd339e6d344819c0afe8ff/openapi.yaml
```
