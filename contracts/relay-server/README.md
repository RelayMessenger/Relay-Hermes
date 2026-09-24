# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `b1e534c03fb9d2826ac63ea0d6cc7a0b843276e9` |
| Source commit date | `2026-09-24T16:03:27-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `b1e534c03fb9d2826ac63ea0d6cc7a0b843276e9/openapi.yaml` |
| Byte length | `243261` |
| SHA-256 | `1a145cd9dbf977de1d4f40191ec861825a19f1c7ab637f60fd507eeea0007402` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/b1e534c03fb9d2826ac63ea0d6cc7a0b843276e9/openapi.yaml
```
