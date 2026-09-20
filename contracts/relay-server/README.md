# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `4fe4a71e20f4f51e5f8db5714c985687c8a63d89` |
| Source commit date | `2026-09-20T16:23:32-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `4fe4a71e20f4f51e5f8db5714c985687c8a63d89/openapi.yaml` |
| Byte length | `175404` |
| SHA-256 | `46eeedd5a5e99e879e32c45972799364021143df9f81acd60837713210639735` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/4fe4a71e20f4f51e5f8db5714c985687c8a63d89/openapi.yaml
```
