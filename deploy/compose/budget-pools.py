#!/usr/bin/env python3
"""DEVELOPMENT_ONLY pools of the Compose placement, through the Agent API (DEC-066).

Control funds every Preparation and Generation from a current platform, tenant and
actor USD pool, and debits a Generation's or Refinement's credits from a current
tenant and actor credit pool; without them it refuses with BUDGET_EXHAUSTED. This
step creates, for the current and the next calendar month (UTC), the USD pools and
the credit pools of the fixture principal (tenant_a, user_a), as the platform
operator principal of api-principals.json, over the platform_operator routes
(POST /api/v1/budget-pools, POST /api/v1/credit-pools). Run by `up.sh budget`
after the services are up.

Idempotent: a pool with the same terms answers 200 and is kept. A pool of the same
scope and period with other terms (409 IDEMPOTENCY_CONFLICT) is kept unchanged as
well and reported; change one with the adjustment routes. Any other answer stops
the run with exit 1. Never prints a token.

    python3 deploy/compose/budget-pools.py <api-principals.json> [--api URL] [--dry-run]
    python3 deploy/compose/budget-pools.py --self-test   # against a fake API that checks contracts/openapi/agent.yaml

Environment (defaults in brackets): ANVILKIT_BUDGET_PLATFORM_CAP [20000000],
ANVILKIT_BUDGET_TENANT_CAP [10000000], ANVILKIT_BUDGET_ACTOR_CAP [10000000]
(scale-6 USD, 1000000 = 1 USD); ANVILKIT_CREDITS_TENANT_LIMIT [100],
ANVILKIT_CREDITS_ACTOR_LIMIT [20] (whole credits).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

TENANT, ACTOR = "tenant_a", "user_a"


def months(now: dt.datetime) -> list[tuple[str, str, str]]:
    """The current and next calendar month as (label, start, end) in UTC."""
    out = []
    year, month = now.year, now.month
    for _ in range(2):
        start = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        end = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
        out.append((start.strftime("%Y-%m"), start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")))
    return out


def amount(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    if not raw.isdigit() or (len(raw) > 1 and raw[0] == "0"):
        raise SystemExit(f"{name} must be a non-negative integer without leading zeros, not {raw!r}")
    return int(raw)


def requests_for(now: dt.datetime) -> list[tuple[str, str, dict]]:
    """Every (route, finder query, body) this step creates."""
    caps = {"platform": amount("ANVILKIT_BUDGET_PLATFORM_CAP", 20_000_000), "tenant": amount("ANVILKIT_BUDGET_TENANT_CAP", 10_000_000),
            "actor": amount("ANVILKIT_BUDGET_ACTOR_CAP", 10_000_000)}
    limits = {"tenant": amount("ANVILKIT_CREDITS_TENANT_LIMIT", 100), "actor": amount("ANVILKIT_CREDITS_ACTOR_LIMIT", 20)}
    out = []
    for label, start, end in months(now):
        for level, cap in caps.items():
            body = {"commandId": f"compose-budget-{level}-{label}", "level": level, "periodStart": start, "periodEnd": end,
                    "cap": {"currency": "USD", "amount": str(cap)}}
            query = f"level={level}&periodStart={start}"
            if level != "platform":
                body["tenantId"] = TENANT
                query += f"&tenantId={TENANT}"
            if level == "actor":
                body["actorId"] = ACTOR
                query += f"&actorId={ACTOR}"
            out.append(("budget-pools", query, body))
        for level, limit in limits.items():
            body = {"commandId": f"compose-credits-{level}-{label}", "level": level, "tenantId": TENANT, "periodStart": start, "periodEnd": end, "limit": limit}
            query = f"level={level}&tenantId={TENANT}&periodStart={start}"
            if level == "actor":
                body["actorId"] = ACTOR
                query += f"&actorId={ACTOR}"
            out.append(("credit-pools", query, body))
    return out


def operator_token(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        principals = json.load(f)
    tokens = [tok for tok, p in principals.items() if "platform_operator" in p.get("roles", [])]
    if len(tokens) != 1:
        raise SystemExit(f"{path}: needs exactly one principal with the platform_operator role, found {len(tokens)} (run up.sh secrets)")
    return tokens[0]


def call(api: str, token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(api + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + token, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw or b"{}")
        except ValueError:
            return err.code, {"error": {"code": "UNPARSEABLE", "message": raw[:200].decode(errors="replace")}}


def describe(route: str, pool: dict) -> str:
    who = "/".join(x for x in (pool.get("level"), pool.get("tenantId"), pool.get("actorId")) if x)
    if route == "budget-pools":
        return f"{who} {pool['periodStart'][:7]} cap {pool['cap']['amount']} allocated {pool['allocated']['amount']}"
    return f"{who} {pool['periodStart'][:7]} limit {pool['limit']} used {pool['used']}"


def run(api: str, token: str, plan: list[tuple[str, str, dict]], emit=print) -> int:
    for route, query, body in plan:
        code, out = call(api, token, "POST", f"/api/v1/{route}", body)
        if code in (200, 201):
            emit(f"{'created' if code == 201 else 'present'}: {describe(route, out)}")
            continue
        if code == 409 and out.get("error", {}).get("code") == "IDEMPOTENCY_CONFLICT":
            code, found = call(api, token, "GET", f"/api/v1/{route}?{query}")
            if code == 200:
                emit(f"kept (other terms): {describe(route, found)}")
                continue
        emit(f"FAIL: POST /api/v1/{route} {body['commandId']}: HTTP {code} {out.get('error', {}).get('code', '')}")
        return 1
    return 0


def self_test() -> int:
    """Runs the step against an in-process fake of the two routes that validates every request and
    answer against contracts/openapi/agent.yaml and answers 201, 200 or 409 as Control does."""
    import http.server
    import tempfile
    import threading
    import urllib.parse

    import yaml
    from jsonschema import Draft4Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT4

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "contracts", "openapi", "agent.yaml"), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    uri = "urn:anvilkit:openapi:agent"
    registry = Registry().with_resource(uri, Resource.from_contents(spec, default_specification=DRAFT4))

    def valid(schema: str, instance: dict) -> bool:
        return Draft4Validator({"$ref": f"{uri}#/components/schemas/{schema}"}, registry=registry).is_valid(instance)

    token, failures, store, fail_route = "op-" + "x" * 30, [], {}, {"route": ""}
    schemas = {"budget-pools": ("CreateBudgetPoolRequest", "BudgetPool"), "credit-pools": ("CreateCreditPoolRequest", "CreditPool")}

    def pool_of(route: str, body: dict) -> dict:
        pool = {k: v for k, v in body.items() if k != "commandId"}
        pool["revision"] = "1"
        if route == "budget-pools":
            pool.update(poolId=f"pool_{len(store)}", allocated={"currency": "USD", "amount": "0"}, available=body["cap"])
        else:
            pool.update(creditPoolId=f"crp_{len(store)}", used=0, available=body["limit"])
        return pool

    class Fake(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def answer(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def route(self) -> tuple[str, dict]:
            url = urllib.parse.urlsplit(self.path)
            if self.headers.get("Authorization") != "Bearer " + token:
                failures.append("a request without the operator's bearer token")
            return url.path.removeprefix("/api/v1/"), dict(urllib.parse.parse_qsl(url.query))

        def do_POST(self):
            route, _ = self.route()
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if route not in schemas or not valid(schemas[route][0], body):
                failures.append(f"POST {route}: the request does not match {schemas.get(route, ('?',))[0]}: {body}")
                return self.answer(400, {"error": {"code": "INVALID_ARGUMENT", "message": "", "requestId": "r", "retryable": False}})
            if route == fail_route["route"]:
                return self.answer(503, {"error": {"code": "DEPENDENCY_UNAVAILABLE", "message": "", "requestId": "r", "retryable": True}})
            key = (route, body["level"], body.get("tenantId"), body.get("actorId"), body["periodStart"])
            fresh = pool_of(route, body)
            if key in store:
                prior = store[key]
                terms = ("cap", "periodEnd") if route == "budget-pools" else ("limit", "periodEnd")
                if any(prior[t] != fresh[t] for t in terms):
                    return self.answer(409, {"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "", "requestId": "r", "retryable": False}})
                return self.answer(200, prior)
            store[key] = fresh
            if not valid(schemas[route][1], fresh):
                failures.append(f"the fake's own {schemas[route][1]} is invalid: {fresh}")
            return self.answer(201, fresh)

        def do_GET(self):
            route, q = self.route()
            key = (route, q.get("level"), q.get("tenantId"), q.get("actorId"), q.get("periodStart"))
            if key not in store:
                return self.answer(404, {"error": {"code": "NOT_FOUND", "message": "", "requestId": "r", "retryable": False}})
            return self.answer(200, store[key])

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{server.server_address[1]}"
    lines: list[str] = []
    december = dt.datetime(2026, 12, 15, tzinfo=dt.timezone.utc)

    def check(cond: bool, what: str) -> None:
        if not cond:
            failures.append(what)

    try:
        check([m[0] for m in months(december)] == ["2026-12", "2027-01"], "the next month after December is January of the next year")
        check(months(december)[1][1:] == ("2027-01-01T00:00:00Z", "2027-02-01T00:00:00Z"), "January's period")
        plan = requests_for(december)
        check(len(plan) == 10, f"two months of three USD and two credit pools, not {len(plan)}")
        check(run(api, token, plan, lines.append) == 0 and sum(x.startswith("created:") for x in lines) == 10, f"a fresh placement creates every pool: {lines}")
        lines.clear()
        check(run(api, token, plan, lines.append) == 0 and all(x.startswith("present:") for x in lines), f"a rerun keeps every pool: {lines}")
        lines.clear()
        os.environ["ANVILKIT_CREDITS_ACTOR_LIMIT"] = "25"
        try:
            check(run(api, token, requests_for(december), lines.append) == 0, "pools with other terms are kept, not a failure")
        finally:
            del os.environ["ANVILKIT_CREDITS_ACTOR_LIMIT"]
        check(sum(x.startswith("kept (other terms): actor/tenant_a/user_a") and "limit 20" in x for x in lines) == 2, f"both actor credit pools are kept and reported: {lines}")
        lines.clear()
        fail_route["route"] = "credit-pools"
        check(run(api, token, plan, lines.append) == 1 and lines[-1].startswith("FAIL: POST /api/v1/credit-pools"), f"an unavailable API stops the run: {lines}")
        check(not any(token in x for x in lines), "the token is never printed")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"a" * 20: {"tenantId": TENANT, "actorId": ACTOR, "roles": ["author"]}, token: {"tenantId": "tenant_ops", "actorId": "operator_a", "roles": ["platform_operator"]}}, f)
        check(operator_token(f.name) == token, "the operator's token is the platform_operator principal's")
        with open(f.name, "w") as g:
            json.dump({"a" * 20: {"tenantId": TENANT, "actorId": ACTOR, "roles": ["author"]}}, g)
        try:
            operator_token(f.name)
            failures.append("a principals file without an operator is accepted")
        except SystemExit:
            pass
        os.unlink(f.name)
        os.environ["ANVILKIT_BUDGET_TENANT_CAP"] = "1.5"
        try:
            requests_for(december)
            failures.append("a non-integer cap is accepted")
        except SystemExit:
            pass
        finally:
            del os.environ["ANVILKIT_BUDGET_TENANT_CAP"]
    finally:
        server.shutdown()
    for f in failures:
        print("FAIL:", f)
    print(f"budget-pools self-test: {len(failures)} failure(s)")
    return 1 if failures else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("principals")
    ap.add_argument("--api", default="http://127.0.0.1:9100")
    ap.add_argument("--dry-run", action="store_true", help="print the requests without sending them")
    args = ap.parse_args()
    plan = requests_for(dt.datetime.now(dt.timezone.utc))
    if args.dry_run:
        for route, _, body in plan:
            print(f"POST /api/v1/{route} {json.dumps(body, sort_keys=True)}")
        return 0
    return run(args.api, operator_token(args.principals), plan, print)


if __name__ == "__main__":
    sys.exit(main())
