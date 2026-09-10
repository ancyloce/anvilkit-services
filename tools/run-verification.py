#!/usr/bin/env python3
"""One entry point for every check in this repository (R04, 2026-09-10).

Runs the derived-artifact builders, the four static checkers and the behavioural proofs added by the
2026-09-10 remediation, in the order the builders require. Each step reports PASS, FAIL or UNEXECUTED;
a step whose environment is missing is reported as UNEXECUTED and counted, never silently skipped, and
the run's exit code is 1 if anything failed and 2 if everything that ran passed but something could not
run. Nothing here contacts a business database, a provider or a deployed environment.

  python3 tools/run-verification.py               # everything
  python3 tools/run-verification.py --static      # builders and static checkers only
  python3 tools/run-verification.py --only sql    # one step

Environment requirements, by step:
  build-*/check-docs/check-contracts/check-ddd  python3 >= 3.11 with jsonschema and pglast
  check-values-proto                            protoc on PATH
  sql                                           docker, and a postgres image (ANVILKIT_PG_IMAGE)
  socket                                        Linux, root, and capsh (libcap2-bin)
  browser                                       Chrome or Chromium (ANVILKIT_CHROME)
  observer                                      node >= 20 and the repository's typescript devDependency
"""
import argparse, os, shutil, subprocess, sys, time, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable

STEPS = [
    ("openapi",  "build",  [PY, "tools/build-agent-openapi.py"],       "rebuild the derived OpenAPI"),
    ("cq",       "build",  [PY, "tools/build-cq-coverage.py"],         "rebuild the CQ coverage index"),
    ("bundle",   "build",  [PY, "tools/refresh-p0-bundle.py"],         "refresh the retained P0 bundle"),
    ("docs",     "static", [PY, "tools/check-docs.py"],                "links, tables, schemas, fixtures"),
    ("proto",    "static", [PY, "tools/check-values-proto.py"],        "values/proto agreement (needs protoc)"),
    ("contracts","static", [PY, "tools/check-contracts.py"],           "cross-contract agreement"),
    ("ddd",      "static", [PY, "tools/check-ddd-contracts.py"],       "DDD models and bundle closure"),
    ("sql",      "proof",  [PY, "tools/verify/sql_control_proof.py"],  "F02/F03/R01/R02 on real PostgreSQL"),
    ("socket",   "proof",  [PY, "tools/verify/socket_proof.py"],       "F01 on real Unix sockets"),
    ("browser",  "proof",  ["node", "tools/verify/browser_proof.mjs"], "F04 in a real browser"),
    ("observer", "proof",  ["node", "tools/verify/observer_proof.mjs"],"F05 static assertion and boundary"),
]


def versions():
    def one(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return (p.stdout + p.stderr).strip().splitlines()[0]
        except Exception:
            return "not available"
    out = [f"python      {sys.version.split()[0]}", f"node        {one(['node','--version'])}",
           f"protoc      {one(['protoc','--version'])}", f"docker      {one(['docker','--version'])}",
           f"psql        {one(['psql','--version'])}",
           f"chrome      {one([os.environ.get('ANVILKIT_CHROME','/usr/bin/google-chrome-stable'),'--version'])}",
           f"capsh       {'present' if shutil.which('capsh') else 'not available'}",
           f"pg image    {os.environ.get('ANVILKIT_PG_IMAGE','postgres:16-alpine')}",
           f"uid         {os.geteuid()} ({'root' if os.geteuid()==0 else 'unprivileged'})"]
    from importlib.metadata import version as dist_version, PackageNotFoundError
    for m in ("jsonschema", "pglast", "referencing"):
        try:
            out.append(f"{m:<11} {dist_version(m)}")
        except PackageNotFoundError:
            out.append(f"{m:<11} not available")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--static", action="store_true", help="builders and static checkers only")
    ap.add_argument("--only", action="append", help="run only the named step(s)")
    ap.add_argument("--quiet", action="store_true", help="print only the step lines and the summary")
    a = ap.parse_args()

    print("environment")
    for line in versions():
        print(f"  {line}")
    print()

    outcomes = []
    for name, kind, cmd, what in STEPS:
        if a.only and name not in a.only: continue
        if a.static and kind == "proof": continue
        t0 = time.time()
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        dt = time.time() - t0
        status = {0: "PASS", 2: "UNEXECUTED"}.get(p.returncode, "FAIL")
        outcomes.append((name, status, p))
        print(f"{status:<11} {name:<10} {what}  ({dt:.1f}s)")
        if status != "PASS" and not a.quiet:
            tail = (p.stdout + p.stderr).strip().splitlines()
            for line in tail[-12:]:
                print(f"            | {line}")

    failed = [n for n, s, _ in outcomes if s == "FAIL"]
    unexec = [n for n, s, _ in outcomes if s == "UNEXECUTED"]
    print(f"\n{len(outcomes)} steps, {len(failed)} failed, {len(unexec)} unexecuted")
    if failed: print("  failed:     " + ", ".join(failed))
    if unexec: print("  unexecuted: " + ", ".join(unexec) + "   (environment missing; NOT a pass)")
    print("\nStatic checks and local proofs establish definition and local behaviour only. "
          "No result here is a design freeze or a target-runtime qualification.")
    return 1 if failed else (2 if unexec else 0)


if __name__ == "__main__":
    sys.exit(main())
