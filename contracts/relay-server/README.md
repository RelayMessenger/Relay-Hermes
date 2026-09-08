# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `99906995625ddc00348064a585ada1649313b0fc` |
| Source commit date | `2026-09-08T08:13:22-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `99906995625ddc00348064a585ada1649313b0fc/openapi.yaml` |
| Byte length | `152428` |
| SHA-256 | `7094178cb01c0ddc05f9254dc91094900a0a7b6273979c0cad6257eec486f0d8` |

The snapshot was copied byte-for-byte from the locked source. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/99906995625ddc00348064a585ada1649313b0fc/openapi.yaml
```
