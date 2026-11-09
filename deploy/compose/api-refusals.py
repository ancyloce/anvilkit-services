#!/usr/bin/env python3
"""API-driven refusal checks of the deployed API (RB-S13, RB-S14, RB-A23).

Run by verify.sh on the server, with the server's own python3 and no third-party
module. Every request below is refused by the API's transport before a handler
runs (request coding, Unicode, schema, route and method rules), so nothing
reaches Control and nothing is created or changed. Each check prints one line in
verify.sh's format (PASS, FAIL) and the exit code is 1 when any check failed.

The bearer token of tenant_a is read from the principals file named by the only
argument and is never printed.

    python3 deploy/compose/api-refusals.py /srv/anvilkit/secrets/api-principals.json [host:port]

The optional address (default 127.0.0.1:9100, the API's loopback publication)
lets the checker itself be tested against a stub.
"""

from __future__ import annotations

import http.client
import json
import sys

API_ADDRESS = "127.0.0.1:9100"
DIGEST = "sha256:" + "0" * 64
MISSING = "op_verify_missing"
CANARY = "CANARY-7f3a<script>x</script>"
# Text that only an echo of the request or of the schema would put in an answer.
LEAKS = ("CANARY", "Schema:", "Value:", "^[A-Za-z0-9]")


def operation(command_id: str) -> str:
    return ('{"commandId":"' + command_id + '","kind":"local_check","subject":{"profileId":"local-check-v1",'
            '"subjectDigest":"' + DIGEST + '"}}')


def command(revision: str) -> str:
    return '{"commandId":"cmd_verify","kind":"cancel","expectedRevision":"' + revision + '"}'


# name, method, path, body (bytes), extra headers, status, code, message, details.field
CASES = [
    ("RB-S14 gzip body", "POST", "/api/v1/operations", operation("c1").encode(), {"Content-Encoding": "gzip"},
     415, "INVALID_ARGUMENT", "compressed request bodies are not accepted", ""),
    ("RB-S14 coding after identity", "POST", "/api/v1/operations", operation("c1").encode(),
     {"Content-Encoding": "identity, br"}, 415, "INVALID_ARGUMENT", "compressed request bodies are not accepted", ""),
    ("RB-A23 invalid UTF-8", "POST", "/api/v1/operations", operation("c\udcff1").encode("utf-8", "surrogateescape"),
     {}, 400, "INVALID_ARGUMENT", "invalid JSON: the body is not valid Unicode", ""),
    ("RB-A23 lone surrogate escape", "POST", "/api/v1/operations", operation("c\\ud8001").encode(), {},
     400, "INVALID_ARGUMENT", "invalid JSON: the body is not valid Unicode", ""),
    ("RB-A23 revision above uint64", "POST", f"/api/v1/operations/{MISSING}/commands",
     command("18446744073709551616").encode(), {}, 400, "INVALID_ARGUMENT", "request body: a value violates format",
     "/expectedRevision"),
    ("RB-A23 SSE cursor above uint64", "GET", f"/api/v1/operations/{MISSING}/events?after=18446744073709551616",
     b"", {}, 400, "INVALID_ARGUMENT", "after must be a canonical sequence", "after"),
    ("RB-A23 no route", "GET", "/api/v1/nothing-here", b"", {}, 404, "NOT_FOUND", "no route serves this path", ""),
    ("RB-A23 no method", "DELETE", "/api/v1/operations", b"", {}, 405, "INVALID_ARGUMENT",
     "this route does not serve the method", ""),
    ("RB-S13 pattern refusal does not echo", "POST", "/api/v1/operations", operation(CANARY).encode(), {},
     400, "INVALID_ARGUMENT", "request body: a value violates pattern", "/commandId"),
    ("RB-S13 undeclared member does not echo", "POST", "/api/v1/operations",
     operation("c1")[:-1].encode() + b',"' + CANARY.encode() + b'":1}', {}, 400, "INVALID_ARGUMENT",
     "request body: a member the contract does not define", ""),
    ("RB-S13 duplicate member does not echo", "POST", "/api/v1/operations",
     ('{"' + CANARY + '":1,"' + CANARY + '":2}').encode(), {}, 400, "INVALID_ARGUMENT",
     "invalid JSON: duplicate member", ""),
]


def tenant_token(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        principals = json.load(f)
    return next(k for k, v in principals.items() if v.get("tenantId") == "tenant_a")


def send(token: str, method: str, path: str, body: bytes, headers: dict):
    host, port = API_ADDRESS.rsplit(":", 1)
    conn = http.client.HTTPConnection(host, int(port), timeout=15)
    try:
        h = {"Authorization": "Bearer " + token}
        if body:
            h["Content-Type"] = "application/json"
        h.update(headers)
        conn.request(method, path, body=body or None, headers=h)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace"), resp.getheader("Retry-After"), resp.getheader("Allow")
    finally:
        conn.close()


def check(token: str, case) -> str | None:
    name, method, path, body, headers, status, code, message, field = case
    got, text, retry_after, allow = send(token, method, path, body, headers)
    try:
        err = json.loads(text)["error"]
    except (ValueError, KeyError, TypeError):
        return f"{got}, not the error envelope: {text[:120]!r}"
    problems = []
    if got != status:
        problems.append(f"status {got} != {status}")
    if err.get("code") != code:
        problems.append(f"code {err.get('code')!r} != {code!r}")
    if err.get("message") != message:
        problems.append(f"message {err.get('message')!r} != {message!r}")
    if (err.get("details") or {}).get("field", "") != field:
        problems.append(f"details.field {(err.get('details') or {}).get('field')!r} != {field!r}")
    if err.get("retryable") is not False:
        problems.append("retryable is not false")
    if not err.get("requestId"):
        problems.append("no requestId")
    if retry_after:
        problems.append("a Retry-After header")
    if status == 405 and "POST" not in (allow or ""):
        problems.append(f"Allow {allow!r} lacks POST")
    leaked = [s for s in LEAKS if s in text]
    if leaked:
        problems.append(f"the answer echoes {leaked}")
    return "; ".join(problems) or None


def main() -> int:
    global API_ADDRESS
    if len(sys.argv) not in (2, 3):
        print("usage: api-refusals.py <api-principals.json> [host:port]", file=sys.stderr)
        return 2
    if len(sys.argv) == 3:
        API_ADDRESS = sys.argv[2]
    try:
        token = tenant_token(sys.argv[1])
    except (OSError, ValueError, StopIteration) as e:
        print(f"FAIL        API refusals: no tenant_a principal ({type(e).__name__})")
        return 1
    rc = 0
    for case in CASES:
        name, method, path, _, _, status = case[:6]
        try:
            problem = check(token, case)
        except OSError as e:
            problem = f"no answer ({type(e).__name__})"
        if problem:
            rc = 1
            print(f"FAIL        API {name}: {method} {path}: {problem}")
        else:
            print(f"PASS        API {name}: {method} {path.split('?')[0]} refused {status} with the envelope, not retryable")
    return rc


if __name__ == "__main__":
    sys.exit(main())
