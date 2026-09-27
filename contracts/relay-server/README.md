# Locked Relay Server OpenAPI

Relay-Hermes release validation uses the immutable snapshot in this directory
instead of checking out another repository during CI or publication.

| Field | Value |
| --- | --- |
| Source repository | `https://github.com/RelayMessenger/Relay-Server` |
| Source commit | `3972ba8aaaae5b958985464f21bfbfbd32f688fb` |
| Source commit date | `2026-09-27T18:11:36-04:00` |
| Source path | `contracts/developer/openapi.yaml` |
| Local snapshot | `openapi.yaml` |
| Byte length | `310606` |
| SHA-256 | `c0214d4a2b302b3c9dbbc4d5cb8fb07808907d22feace58025ab7377d423515b` |

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
