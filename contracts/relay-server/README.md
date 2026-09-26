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
| Byte length | `243571` |
| SHA-256 | `57c553a8b0d281ed5e815fc08184285315fcf9a85bedde92628f3ed33235b15d` |

The snapshot was copied byte-for-byte from the locked source, then its
`SelectionPart` and `SelectionPartResponse` were edited to the owner-approved
selection `title` shape (Relay `_artifacts/selection-title-20260926/SPEC.md`,
2026-09-26) ahead of the Relay-Server commit that ships it; re-lock to that
commit once it lands. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/b1e534c03fb9d2826ac63ea0d6cc7a0b843276e9/openapi.yaml
```
