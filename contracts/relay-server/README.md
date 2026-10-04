# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `ed5608a17f35ce6e87bf8f66b6737157c13820b5` |
| Source commit date | `2026-10-04T16:26:02-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `openapi.yaml` |
| Byte length | `361294` |
| SHA-256 | `d718bf72bef79074ebab8110da7cd42553e151bee86336f1384d5bfb352ef7fb` |

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
