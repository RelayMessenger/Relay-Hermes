# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `a25111520f7fc92c25ecd945d1dfc9afa9f60a1f` |
| Source commit date | `2026-09-20T21:47:12-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `a25111520f7fc92c25ecd945d1dfc9afa9f60a1f/openapi.yaml` |
| Byte length | `175262` |
| SHA-256 | `9f3e662a13cd0e6b16a52fba4b53c75fe5817d134dcf152e00b054699c37839c` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/a25111520f7fc92c25ecd945d1dfc9afa9f60a1f/openapi.yaml
```
