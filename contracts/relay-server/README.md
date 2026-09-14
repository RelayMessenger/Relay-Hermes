# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `d4dc62372194bf929801229740346cdacfe2d5c9` |
| Source commit date | `2026-09-13T16:12:32-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `d4dc62372194bf929801229740346cdacfe2d5c9/openapi.yaml` |
| Byte length | `145119` |
| SHA-256 | `81d23529476ae77b3b7f7dfc931d2e0e421d3c91e20c59136e2deef9123f722e` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/d4dc62372194bf929801229740346cdacfe2d5c9/openapi.yaml
```

The prior snapshot `1a2245dd775f781b57e0d1f6f3146ebd384c90c3/openapi.yaml` is retained unchanged as historical evidence. It is not the current CI or release input.
