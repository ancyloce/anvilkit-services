#!/usr/bin/env python3
"""Operator recovery through the public API on the deployed release (F-P3.7-1, API-17).

  .local/verification-venv/bin/python deploy/qualification/recovery-demo.py --run RUN [--wait 600]

Precondition: the environment converged on a release with API-17, and bootstrap.py has
written the DEVELOPMENT_ONLY operator principals (RECOVERY_OPERATORS) to OpenBao. The API
reads its principals file once at start: when the operator is still unknown (401) the
demo waits for the CSI driver's rotation of the synced principals Secret and restarts the
API Deployment (a rolling restart under its PodDisruptionBudget).

Everything goes through the drills' NodePort on a live worker, the way a client reaches
the API, with the principals' bearer tokens (never printed, never in the evidence):
  * identity and authorization: no token 401; an author (tenant_a) 403 on begin, and 404 on
    the run and on a disposition (Control discloses a run only to its own tenant's
    operators and answers NOT_FOUND to anyone else, as TestRecoveryReconciliation shows; a
    same-tenant non-operator disposition needs a principal this environment does not
    have and stays with the API and Control unit tests); an operator asking for every
    tenant without platform_operator 403; a tenant named in the body 400 (the scope is the
    principal's tenant);
  * begin and idempotency: the operator of tenant_recovery begins a run over a past
    window (201), the same command again answers 200 with the same run, the same command
    with another body 409 IDEMPOTENCY_CONFLICT, a second command while the run is open
    409 STALE_EXECUTION (recorded as not applicable when the first run had already
    reopened);
  * tenant boundary: tenant_b's operator gets 404 on the run, its findings and a
    disposition;
  * the run reaches a terminal phase (reopened or restricted) and its findings are read;
  * audit: Control's recovery_runs row names the operator's tenant, actor and command.
tenant_recovery owns no operation, so the run fences and reconciles nothing another
tenant or a drill uses. A disposition with an invented obligation is refused (400 without
evidence); no real disposition is made.
Writes outputs/qualification/<RUN>/recovery/recovery-demo.json. Exit 0 when every check
holds, 1 otherwise, 2 when the environment or the operator principals are missing.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bootstrap  # noqa: E402
import drills  # noqa: E402

ROOT = drills.ROOT
STATE = drills.STATE


def call(token: str | None, method: str, path: str, body: dict | None = None, timeout: float = 15.0, api: bool = True) -> tuple[int, dict | None]:
    """One request through the drill NodePort on the first live worker that answers; path is
    under /api/v1 unless api is False."""
    last = (0, None)
    for node in sorted(drills.workers()):
        if drills.docker("inspect", "-f", "{{.State.Running}}", node) != "true":
            continue
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(f"http://{drills.node_ip(node)}:{PORT}{'/api/v1' if api else ''}{path}", method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, None
        except OSError:
            last = (0, None)
    return last


def code(parsed: dict | None) -> str | None:
    return ((parsed or {}).get("error") or {}).get("code")


def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def converged() -> bool:
    """Every Application Synced and Healthy (read only: unlike drills.apps_healthy, no
    OpenBao unseal and no credential back-fill)."""
    apps = json.loads(drills.k("-n", "anvilkit-platform", "get", "applications.argoproj.io", "-o", "json"))["items"]
    return bool(apps) and all(x.get("status", {}).get("sync", {}).get("status") == "Synced"
                              and x.get("status", {}).get("health", {}).get("status") == "Healthy" for x in apps)


def principals_synced(tokens: list[str]) -> bool:
    """Whether the API's synced principals Secret holds every token (compared by digest)."""
    raw = drills.k("-n", "anvilkit-apps", "get", "secret", "anvilkit-agent-api-principals", "-o", "jsonpath={.data.principals\\.json}", check=False)
    if not raw.strip():
        return False
    held = {hashlib.sha256(t.encode()).hexdigest() for t in json.loads(base64.b64decode(raw))}
    return all(hashlib.sha256(t.encode()).hexdigest() in held for t in tokens)


PORT = 0


def main() -> int:
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--wait", type=int, default=600, help="seconds the run may take to reach a terminal phase")
    a = ap.parse_args()
    creds = json.loads((STATE / "credentials.json").read_text())
    p = creds["principals"]
    if any(name not in p for name in bootstrap.RECOVERY_OPERATORS):
        print("UNEXECUTED: the operator principals are not in the credentials; run deploy/qualification/bootstrap.py first")
        return 2
    if not drills.wait_until(converged, 300, 15):
        print("UNEXECUTED: the environment is not converged")
        return 2
    PORT = drills.drill_service()
    if not drills.wait_until(lambda: 200 <= call(None, "GET", "/readyz", api=False)[0] < 300, 120, 2):
        print("UNEXECUTED: the API does not answer through the drill NodePort")
        return 2
    operator, other, author = p["recoveryOperator"], p["operatorB"], p["tenantA"]
    out = ROOT / "outputs/qualification" / a.run / "recovery"
    out.mkdir(parents=True, exist_ok=True)
    checks: dict[str, bool | None] = {}
    observed: dict[str, object] = {"responses": {}}

    def expect(check: str, got: tuple[int, dict | None], status: int, error: str | None = None) -> tuple[int, dict | None]:
        observed["responses"][check] = {"status": got[0], "code": code(got[1])}
        checks[check] = got[0] == status and (error is None or code(got[1]) == error)
        return got
    started = time.time()

    # The API knows the operators only after a start that read the rotated Secret.
    if call(operator, "GET", "/recoveries/rcv_unknown")[0] == 401:
        if not drills.wait_until(lambda: principals_synced([operator, other]), 300, 10):
            print("UNEXECUTED: the synced principals Secret does not hold the operators (OpenBao payload or CSI rotation)")
            return 2
        drills.k("-n", "anvilkit-apps", "rollout", "restart", "deployment/anvilkit-agent-api")
        drills.k("-n", "anvilkit-apps", "rollout", "status", "deployment/anvilkit-agent-api", "--timeout=300s", timeout=320)
        observed["apiRestarted"] = True
    checks["operatorAuthenticated"] = drills.wait_until(lambda: call(operator, "GET", "/recoveries/rcv_unknown")[0] == 404, 120, 3) is not None

    end = time.time() - 60
    begin = {"commandId": f"cmd_recovery_demo_{int(started)}", "windowStart": stamp(end - 3600), "windowEnd": stamp(end),
             "clockUncertaintySeconds": 5, "reason": "qualification API-17 demo (tenant_recovery owns no operation)"}

    expect("noTokenRefused401", call(None, "POST", "/recoveries", begin), 401)
    expect("authorRefused403", call(author, "POST", "/recoveries", begin), 403)
    expect("allTenantsWithoutPlatformOperator403", call(operator, "POST", "/recoveries", {**begin, "allTenants": True}), 403)
    expect("tenantInBody400", call(operator, "POST", "/recoveries", {**begin, "tenantId": "tenant_a"}), 400)

    s, run = expect("begin201", call(operator, "POST", "/recoveries", begin), 201)
    run_id = (run or {}).get("runId")
    observed["begin"] = {"status": s, "runId": run_id, "scopeKey": (run or {}).get("scopeKey"), "phase": (run or {}).get("phase"),
                         "recoveryEpoch": (run or {}).get("recoveryEpoch")}
    if not run_id:
        observed["beginError"] = code(run)
        return finish(out, a.run, checks, observed, started)
    checks["scopeIsPrincipalTenant"] = (run or {}).get("scopeKey") == "tenant_recovery"
    s, again = call(operator, "POST", "/recoveries", begin)
    observed["responses"]["repeat200SameRun"] = {"status": s, "sameRun": (again or {}).get("runId") == run_id}
    checks["repeat200SameRun"] = s == 200 and (again or {}).get("runId") == run_id
    expect("changedBody409IdempotencyConflict", call(operator, "POST", "/recoveries", {**begin, "reason": "changed"}), 409, "IDEMPOTENCY_CONFLICT")
    s, cur = call(operator, "GET", f"/recoveries/{run_id}")
    if (cur or {}).get("phase") in ("reopened", "restricted"):
        checks["secondRun409StaleExecution"] = None
        observed["secondRun"] = "not applicable: the first run had already reached a terminal phase"
    else:
        expect("secondRun409StaleExecution", call(operator, "POST", "/recoveries", {**begin, "commandId": begin["commandId"] + "_second"}),
               409, "STALE_EXECUTION")

    expect("otherTenantRun404", call(other, "GET", f"/recoveries/{run_id}"), 404)
    expect("otherTenantFindings404", call(other, "GET", f"/recoveries/{run_id}/findings"), 404)
    expect("authorRead404", call(author, "GET", f"/recoveries/{run_id}"), 404)

    terminal = None
    deadline = time.time() + a.wait
    phases = []
    while time.time() < deadline:
        s, cur = call(operator, "GET", f"/recoveries/{run_id}")
        ph = (cur or {}).get("phase")
        if ph and (not phases or phases[-1] != ph):
            phases.append(ph)
        if ph in ("reopened", "restricted"):
            terminal = cur
            break
        time.sleep(5)
    observed["phases"] = phases
    observed["terminal"] = {k: (terminal or {}).get(k) for k in ("phase", "fencedOperations", "unsettledFindings", "reopenedAt")}
    checks["reachedTerminalPhase"] = terminal is not None
    # Sequences are decimal strings in the public contract.
    checks["reopenedWithoutUnsettledFindings"] = (terminal or {}).get("phase") == "reopened" and str((terminal or {}).get("unsettledFindings")) == "0"

    s, page = call(operator, "GET", f"/recoveries/{run_id}/findings?limit=50")
    checks["findingsRead200"] = s == 200 and isinstance((page or {}).get("findings"), list)
    observed["findings"] = {"count": len((page or {}).get("findings") or []), "complete": (page or {}).get("complete"),
                            "statuses": sorted({f.get("status") for f in (page or {}).get("findings") or []})}

    # Valid in every field but the evidence (evidenceRef and evidenceDigest are required).
    disposition = {"commandId": f"cmd_disp_demo_{int(started)}", "class": "model-dispatch", "obligationId": "obl_demo_unknown",
                   "decision": "confirm_not_sent", "recoveryEpoch": str((run or {}).get("recoveryEpoch") or "1")}
    expect("dispositionWithoutEvidence400", call(operator, "POST", f"/recoveries/{run_id}/dispositions", disposition), 400)
    # The authorization checks send a request Control's argument checks accept (an
    # obligation without a restorable identity admits only retain_exposure; those checks
    # read nothing of the run), so only the role and the tenant can refuse it.
    complete = {**disposition, "decision": "retain_exposure", "evidenceRef": "demo://none", "evidenceDigest": "sha256:" + "0" * 64}
    expect("authorDispositionOtherTenant404", call(author, "POST", f"/recoveries/{run_id}/dispositions", complete), 404)
    expect("otherTenantDisposition404", call(other, "POST", f"/recoveries/{run_id}/dispositions", complete), 404)

    # Audit: Control's own record of who began the run, read-only.
    row = drills.psql("anvilkit-business", "SELECT tenant_id, actor_id, command_id, scope_key FROM recovery_runs "
                                           f"WHERE run_id = '{run_id}'", db="anvilkit_control")
    fields = row.split("|") if row else []
    observed["audit"] = dict(zip(("tenantId", "actorId", "commandId", "scopeKey"), fields))
    checks["auditNamesOperator"] = fields == ["tenant_recovery", "operator_recovery", begin["commandId"], "tenant_recovery"]
    return finish(out, a.run, checks, observed, started)


def finish(out: pathlib.Path, run: str, checks: dict, observed: dict, started: float) -> int:
    failed = sorted(k for k, v in checks.items() if v is False)
    result = {"schemaVersion": 1, "run": run, "environmentRevision": drills.environment_revision(),
              "startedAt": stamp(started), "finishedAt": stamp(time.time()), "checks": checks, "observed": observed,
              "status": "FAIL" if failed else "PASS", "failed": failed,
              "placement": "DEVELOPMENT_ONLY (fixture bearer principals; production identity is ENV-07)"}
    (out / "recovery-demo.json").write_text(json.dumps(result, indent=2) + "\n")
    for k, v in checks.items():
        print(f"{'PASS' if v else ('N/A ' if v is None else 'FAIL')}  {k}")
    print(f"recovery demo {result['status']}: {out.relative_to(ROOT) / 'recovery-demo.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
