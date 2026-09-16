# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `35023fe4f52497f2c27fb9172a5f0b27a7be8bf1` |
| Source commit date | `2026-09-16T13:55:11-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `35023fe4f52497f2c27fb9172a5f0b27a7be8bf1/openapi.yaml` |
| Byte length | `146181` |
| SHA-256 | `42e8039ee94377aa047f70593102bed980d00c3597f6850a0628cbd0fdf6bc81` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/35023fe4f52497f2c27fb9172a5f0b27a7be8bf1/openapi.yaml
```
