#!/usr/bin/env python3
"""Development smoke helper: reads the DeepSeek API key the owner entered with
deploy/verification/openbao/enter-deepseek.py (kv/anvilkit/local/model-proxy/deepseek,
field api_key) from the owned local OpenBao over its verified TLS tunnel
(127.0.0.1:18200) and writes it to the 0600 file named as the only argument,
for the Model Proxy process alone. Prints status lines only, never the key.

  .local/verification-venv/bin/python tools/smoke/fetch-deepseek-key.py <out-file>

Exit 0 when the file was written, 2 when OpenBao is sealed, unreachable or
holds no key (the owner unseals with deploy/verification/openbao/restore-existing.py
and enters the key with enter-deepseek.py).
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/verification/openbao"))
from bootstrap import STATE, request  # noqa: E402  (the owned local OpenBao client)

PATH = "anvilkit/local/model-proxy/deepseek"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2
    out = pathlib.Path(sys.argv[1])
    os.umask(0o077)
    try:
        status = request("sys/seal-status")
    except Exception as error:  # the tunnel or the API
        print(f"UNEXECUTED: OpenBao unreachable at the local tunnel: {error}")
        return 2
    if not status.get("initialized") or status.get("sealed"):
        print("UNEXECUTED: the dev OpenBao is sealed (owner: deploy/verification/openbao/restore-existing.py)")
        return 2
    material = json.loads((STATE / "init.json").read_text())
    root = material["root_token"]
    del material
    try:
        token = request("auth/token/create", {"policies": ["default"], "ttl": "2m", "num_uses": 2}, root)["auth"]["client_token"]
    except RuntimeError:
        token = root  # no scoped read policy exists; the one read then uses the bootstrap token directly
    try:
        data = request(f"kv/data/{PATH}", token=token)
    except RuntimeError as error:
        if token != root:
            try:
                data = request(f"kv/data/{PATH}", token=root)
            except RuntimeError as error2:
                print(f"UNEXECUTED: no readable key at kv/{PATH}: {error2}")
                return 2
        else:
            print(f"UNEXECUTED: no readable key at kv/{PATH}: {error}")
            return 2
    key = (data.get("data", {}).get("data", {}) or {}).get("api_key", "")
    if not key or any(c.isspace() for c in key):
        print(f"UNEXECUTED: kv/{PATH} holds no api_key")
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)  # no trailing newline: a Kubernetes Secret created from the file carries the bytes exactly
    print(f"credential written to {out} (0600, {len(key)} characters, version {data.get('data', {}).get('metadata', {}).get('version')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
