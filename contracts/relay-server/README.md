# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `65c26f166e1011be50205737b6f9273a50f08ee0` |
| Source commit date | `2026-09-30T22:59:59-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `openapi.yaml` |
| Byte length | `339872` |
| SHA-256 | `106c738d4152b65be03f32938d89d9a433f87156478b0c8ce65abbd70ad6a1c9` |

The snapshot was copied byte-for-byte from the locked source. It sits at a
short path on purpose: Hermes copies the whole plugin into a dependency
workspace several directories deep, and on Windows a path over 260
characters fails with WinError 206. The source commit is recorded above, not
in the path. Validate both its
identity and the Relay-Hermes contract expectations from the repository root:

```sh
python scripts/check-openapi.py \
  contracts/relay-server/openapi.yaml
```
