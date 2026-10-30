#!/usr/bin/env python3
"""Model route bounds agree across the Compose placement and the codegen team (DEC-030).

  .local/verification-venv/bin/python tools/check-model-routes.py              # exit 1 on a disagreement
  .local/verification-venv/bin/python tools/check-model-routes.py --self-test

The codegen team's configuration (jobs/codegen/team/team.yaml) and the Compose configuration
(deploy/compose/config) state these bounds in prose comments; nothing checked them, and a
disagreement showed only on the server as a model call cut at its output cap (B1 runs 2 and 4,
plan 0005 §11.7 and §11.9):
  route     the team's route (team.yaml route.id) and the Preparation's route
            (lifecycle.preparation.route_id of the Compose Workflow configuration, else the
            Workflow's built-in default) are enabled routes of the Compose Model Proxy;
  tokens    team.yaml route.maxTokens, what Pi is told about the route, equals the Proxy
            route's limits.max_output_tokens;
  exposure  team.yaml route.exposurePerSend and currency equal the Proxy route's
            limits.max_exposure, which the Proxy reserves for every send;
  brief     the Preparation writes the frozen brief in one call, so its max_output_tokens
            (the Compose value, else the Workflow's built-in default) equals its route's
            limits.max_output_tokens (DEC-024).
The planner's cap equals team.yaml route.maxTokens; the team's own test (test/prompts.test.ts)
checks that, so with `tokens` the planner holds the Proxy's bound too.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml")
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEAM = "jobs/codegen/team/team.yaml"
PROXY = "deploy/compose/config/anvilkit-agent-model-proxy.yaml"
WORKFLOW = "deploy/compose/config/anvilkit-agent-workflow.yaml"
WORKFLOW_DEFAULTS = "services/agent/workflow/internal/config/config.go"
INPUTS = (TEAM, PROXY, WORKFLOW, WORKFLOW_DEFAULTS)


def workflow_default(go_source: str, key: str) -> str | None:
    """A built-in default of the Workflow's configuration (its koanf defaults map)."""
    m = re.search(r'"' + re.escape(key) + r'":\s*("([^"]*)"|\d+)', go_source)
    return None if not m else (m.group(2) if m.group(2) is not None else m.group(1))


def check(root: pathlib.Path) -> list[str]:
    missing = [rel for rel in INPUTS if not (root / rel).is_file()]
    if missing:
        return [f"input missing: {rel}" for rel in missing]
    team = yaml.safe_load((root / TEAM).read_text())
    proxy = yaml.safe_load((root / PROXY).read_text())
    workflow = yaml.safe_load((root / WORKFLOW).read_text()) or {}
    defaults = (root / WORKFLOW_DEFAULTS).read_text()
    routes = {r.get("id"): r for r in proxy.get("routes") or []}
    errs: list[str] = []

    def route(route_id: str, user: str) -> dict | None:
        r = routes.get(route_id)
        if r is None:
            errs.append(f"route: {user} uses {route_id!r}, which {PROXY} does not declare")
        elif r.get("enabled") is not True:
            errs.append(f"route: {user} uses {route_id!r}, which {PROXY} does not enable")
        return r

    tr = team["route"]
    r = route(tr["id"], f"{TEAM} route.id")
    if r is not None:
        limits = r.get("limits") or {}
        if tr["maxTokens"] != limits.get("max_output_tokens"):
            errs.append(f"tokens: {TEAM} route.maxTokens {tr['maxTokens']} differs from {PROXY} {tr['id']} "
                        f"limits.max_output_tokens {limits.get('max_output_tokens')}")
        exposure = limits.get("max_exposure") or {}
        if (str(tr["exposurePerSend"]), team.get("currency")) != (str(exposure.get("amount")), exposure.get("currency")):
            errs.append(f"exposure: {TEAM} route.exposurePerSend {tr['exposurePerSend']} {team.get('currency')} differs from "
                        f"{PROXY} {tr['id']} limits.max_exposure {exposure.get('amount')} {exposure.get('currency')}")

    preparation = (workflow.get("lifecycle") or {}).get("preparation") or {}
    prep_route = preparation.get("route_id") or workflow_default(defaults, "lifecycle.preparation.route_id")
    prep_tokens = preparation.get("max_output_tokens")
    source = WORKFLOW
    if prep_tokens is None:
        prep_tokens, source = workflow_default(defaults, "lifecycle.preparation.max_output_tokens"), f"the Workflow default ({WORKFLOW_DEFAULTS})"
    if prep_route is None or prep_tokens is None:
        errs.append(f"brief: the Preparation's route or max_output_tokens is neither in {WORKFLOW} nor a default in {WORKFLOW_DEFAULTS}")
    else:
        r = route(prep_route, "the Preparation")
        bound = ((r or {}).get("limits") or {}).get("max_output_tokens")
        if r is not None and int(prep_tokens) != bound:
            errs.append(f"brief: the Preparation's max_output_tokens {prep_tokens} ({source}) differs from {PROXY} {prep_route} "
                        f"limits.max_output_tokens {bound}; it writes the frozen brief in one call")
    return errs


def self_test() -> int:
    """The checkout passes; each rule fails on the change that broke it on the server, or would."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="check-model-routes-") as tmp:
        base = pathlib.Path(tmp)
        for rel in INPUTS:
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / rel, base / rel)
        if errs := check(base):
            failures.append(f"baseline: the checkout must pass ({errs})")

        def mutated(rel: str, old: str, new: str, rule: str, why: str) -> None:
            text = (base / rel).read_text()
            if old not in text:
                failures.append(f"{why}: {rel} no longer contains {old!r}")
                return
            (base / rel).write_text(text.replace(old, new, 1))
            if not any(e.startswith(rule + ":") for e in check(base)):
                failures.append(f"{why}: no {rule} failure")
            (base / rel).write_text(text)

        mutated(WORKFLOW, "    max_output_tokens: 4096\n", "", "brief", "the Compose Workflow without its Preparation cap (B1 run 4)")
        mutated(PROXY, "    max_output_tokens: 4096", "    max_output_tokens: 2048", "tokens", "a Proxy bound below team.yaml route.maxTokens")
        mutated(TEAM, 'exposurePerSend: "1000000"', 'exposurePerSend: "2000000"', "exposure", "team.yaml reserving another exposure")
        mutated(PROXY, "  enabled: true\n  health_probe:", "  enabled: false\n  health_probe:", "route", "the team's route disabled")
    for f in failures:
        print("FAIL", f)
    print(f"check-model-routes self-test: {'PASS' if not failures else 'FAIL'} (baseline and 4 negatives)")
    return 1 if failures else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    if sys.argv[1:]:
        print(__doc__)
        return 2
    errs = check(ROOT)
    for e in errs:
        print("FAIL", e)
    print(f"check-model-routes: {len(errs)} failures")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
