# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `1a2245dd775f781b57e0d1f6f3146ebd384c90c3` |
| Source commit date | `2026-09-08T18:30:51-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `1a2245dd775f781b57e0d1f6f3146ebd384c90c3/openapi.yaml` |
| Byte length | `156157` |
| SHA-256 | `5458497fe8db4ee7dfe6bef67f2803137575d3ea4d835748290a5c9f8d906791` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/1a2245dd775f781b57e0d1f6f3146ebd384c90c3/openapi.yaml
```
