#!/usr/bin/env python3
"""Disposable source-export exercise of the replacement baseline (delivery.md P01 exit).

Usage: python3 tools/check-source-export.py [--keep DIR] [--skip-build]

A `.gitignore` edit is not evidence that the design and build inputs are included in the
source. This check computes the export set the way Git would: the parent's tracked files
plus its untracked files that are not ignored, and, for each replacement repository mounted
in the tree (the contracts repository at contracts/, the API, Control and Workflow
repositories at services/agent/{api,control,workflow,model-proxy}, since P14 the Knowledge
and MCP repositories at services/agent/{knowledge,mcp}, the access sidecar repository at
jobs/shared/access-sidecar and the validator repository at jobs/validator, whether already
registered as submodules or still nested checkouts), that repository's own tracked plus
untracked-not-ignored files. The Background Worker (services/agent/background-worker) and the
shared configuration schemas (packages/profile-schemas) are plain directories of the parent
until their repositories exist.
Then:

  1. Required inputs: every file under each declared input directory and every anchor file
     (contract sources and generated bindings in contracts/, migrations, the Go modules of the
     replacement, the dev foundation, the current documentation, the verification tools) is in
     the export set.
  2. Exclusions: nothing from docs/archive/, outputs/, logs/, .local/, node_modules/,
     __pycache__/ or *.egg-info/, no `.env*`, `*.pem`, `*.log`, `*.kubeconfig`, and nothing
     from inside a legacy submodule.
  3. Disposable export: the export set is copied into a temporary directory and checked
     there on its own: tools/check-docs.py, tools/check-contracts.py and `go build ./...`
     in every go.work module (GOFLAGS=-mod=readonly), so the export is self-contained.

Legacy environment tooling that the cleanup record keeps (deploy/local/, compose.yaml,
tools/prepare-local-compose.py, tools/component/) is reported, not required and not failed.
Git is only read; the export lives in a temporary directory unless --keep names one.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pyenv_check  # noqa: E402

# The disposable export runs check-contracts.py with this interpreter.
pyenv_check.require("yaml", "jsonschema", "referencing", "openapi_spec_validator")

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Repositories of the replacement mounted inside this tree: their content is source,
# listed through their own Git. Every other gitlink is a submodule without an
# implementation in this closure (excluded).
REPLACEMENT_REPOS = ("contracts", "services/agent/api", "services/agent/control", "services/agent/workflow", "services/agent/model-proxy", "services/agent/knowledge", "services/agent/mcp", "jobs/shared/access-sidecar", "jobs/validator")
REQUIRED_DIRS = (
    "contracts/proto",
    "contracts/openapi",
    "contracts/jobs",
    "contracts/components",
    "contracts/events",
    "contracts/go",
    "contracts/ts/src",
    "contracts/ts/test",
    "contracts/python",
    "contracts/tests/go",
    "contracts/tools",
    "docs/architecture",
    "jobs/codegen",
    "jobs/codegen/team/src",
    "jobs/codegen/team/test",
    "jobs/codegen/agent/team/prompts",
    "jobs/migration",
    "jobs/shared/access-sidecar",
    "jobs/validator/src",
    "jobs/validator/test",
    "jobs/validator/profiles",
    "jobs/validator/fixtures",
    "deploy/policies",
    "services/agent/api",
    "services/agent/control",
    "services/agent/workflow",
    "services/agent/model-proxy/src",
    "services/agent/model-proxy/test",
    "services/agent/model-proxy/deploy",
    "services/agent/knowledge/src",
    "services/agent/knowledge/test",
    "services/agent/knowledge/forwarder",
    "services/agent/knowledge/deploy",
    "services/agent/mcp/cmd",
    "services/agent/mcp/internal",
    "services/agent/mcp/deploy",
    "services/agent/background-worker/src",
    "services/agent/background-worker/test",
    "services/agent/background-worker/deploy",
    "packages/profile-schemas",
    "tests/contracts",
    "tests/integration",
    "deploy/dev",
)
REQUIRED_FILES = (
    "README.md", "AGENTS.md", "CLAUDE.md", "docs/README.md", "go.work", "go.work.sum",
    "contracts/README.md", "contracts/LICENSE", "contracts/package.json", "contracts/pnpm-workspace.yaml", "contracts/pnpm-lock.yaml",
    "contracts/buf.yaml", "contracts/buf.gen.yaml", "contracts/buf.gen.ts.yaml", "contracts/buf.lock",
    "contracts/openapi/agent.yaml", "contracts/openapi/inference.yaml", "contracts/openapi/model-proxy.yaml",
    "contracts/openapi/oapi-codegen.yaml", "contracts/openapi/oapi-codegen.model-proxy.yaml",
    "contracts/openapi/agent.fixtures.json", "contracts/openapi/inference.fixtures.json", "contracts/openapi/model-proxy.fixtures.json",
    "contracts/proto/anvilkit/control/v1/control.proto", "contracts/proto/anvilkit/control/v1/dispatch.proto",
    "contracts/proto/anvilkit/control/v1/artifact.proto", "contracts/proto/anvilkit/knowledge/v1/knowledge.proto",
    "contracts/proto/anvilkit/mcp/v1/mcp.proto", "contracts/proto/messages.fixtures.json",
    "contracts/jobs/job.schema.json", "contracts/jobs/profiles.json", "contracts/jobs/fixtures.json",
    "contracts/components/component.schema.json", "contracts/components/fixtures.json",
    "contracts/events/events.schema.json", "contracts/events/fixtures.json",
    "contracts/go/go.mod", "contracts/go/go.sum", "contracts/go/README.md",
    "contracts/ts/package.json", "contracts/ts/tsconfig.json", "contracts/ts/biome.json",
    "contracts/python/pyproject.toml", "contracts/tests/go/go.mod",
    "contracts/tools/generate.py", "contracts/tools/check.py", "contracts/tools/verify.py",
    "contracts/tools/pyenv_check.py", "contracts/tools/requirements.txt", "contracts/tools/verification-env.sh",
    "jobs/codegen/go.mod", "jobs/codegen/go.sum", "jobs/codegen/config.yaml", "jobs/codegen/Dockerfile", "jobs/codegen/README.md",
    "jobs/codegen/agent/resources.json", "jobs/codegen/fixtures/fixed-input.txt",
    "jobs/codegen/Dockerfile.team", "jobs/codegen/team.yaml", "jobs/codegen/fixtures/brief.json", "jobs/codegen/agent/team/tools.json",
    "jobs/codegen/team/package.json", "jobs/codegen/team/pnpm-lock.yaml", "jobs/codegen/team/pnpm-workspace.yaml",
    "jobs/codegen/team/tsconfig.json", "jobs/codegen/team/tsconfig.build.json", "jobs/codegen/team/biome.json",
    "jobs/codegen/team/vitest.config.ts", "jobs/codegen/team/README.md",
    "jobs/shared/access-sidecar/go.mod", "jobs/shared/access-sidecar/go.sum", "jobs/shared/access-sidecar/config.yaml",
    "jobs/shared/access-sidecar/Dockerfile", "jobs/shared/access-sidecar/README.md", "jobs/shared/access-sidecar/LICENSE",
    "jobs/validator/package.json", "jobs/validator/pnpm-lock.yaml", "jobs/validator/pnpm-workspace.yaml", "jobs/validator/tsconfig.json",
    "jobs/validator/tsconfig.build.json", "jobs/validator/biome.json", "jobs/validator/vitest.config.ts", "jobs/validator/config.yaml",
    "jobs/validator/Dockerfile", "jobs/validator/README.md", "jobs/validator/LICENSE",
    "deploy/policies/kyverno/anvilkit-components-jobs.yaml", "deploy/policies/kyverno/anvilkit-components-registries.dev.yaml",
    "deploy/policies/seccomp/anvilkit-candidate.json", "deploy/policies/seccomp/generate.sh",
    "deploy/policies/network/anvilkit-components-egress.yaml", "deploy/policies/check.sh", "deploy/policies/README.md",
    "deploy/dev/images.sh", "deploy/dev/docker/access-sidecar.dev.Dockerfile",
    "jobs/migration/internal/migrate/sql/knowledge/00001_init.sql",
    "jobs/migration/internal/migrate/sql/knowledge/00002_background.sql",
    "jobs/migration/internal/migrate/sql/mcp/00001_init.sql",
    "jobs/migration/internal/migrate/sql/mcp/00002_background.sql",
    "package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml",
    "services/agent/api/go.mod", "services/agent/api/go.sum", "services/agent/api/config.yaml",
    "services/agent/api/README.md", "services/agent/api/LICENSE", "services/agent/api/Dockerfile", "services/agent/api/.dockerignore",
    "services/agent/api/deploy/chart/Chart.yaml", "services/agent/api/deploy/chart/values.yaml",
    "services/agent/api/.github/workflows/ci.yml",
    "services/agent/control/go.mod", "services/agent/control/go.sum", "services/agent/control/config.yaml", "services/agent/control/sqlc.yaml",
    "services/agent/control/README.md", "services/agent/control/LICENSE", "services/agent/control/Dockerfile", "services/agent/control/.dockerignore",
    "services/agent/control/tools/sqlc.sh", "services/agent/control/.github/workflows/ci.yml",
    "services/agent/control/deploy/chart/Chart.yaml", "services/agent/control/deploy/chart/values.yaml",
    "services/agent/control/internal/migrate/sql/00001_init.sql",
    "services/agent/control/internal/migrate/sql/00002_stage_manifest_bytes.sql",
    "services/agent/control/internal/migrate/sql/00003_dispatch_denial_code.sql",
    "services/agent/control/internal/migrate/sql/00004_usage_observation_presence.sql",
    "services/agent/control/internal/migrate/sql/00005_effects_and_recovery.sql",
    "services/agent/control/internal/migrate/sql/00006_artifacts.sql",
    "services/agent/workflow/go.mod", "services/agent/workflow/go.sum", "services/agent/workflow/config.yaml",
    "services/agent/workflow/README.md", "services/agent/workflow/LICENSE", "services/agent/workflow/Dockerfile", "services/agent/workflow/.dockerignore",
    "services/agent/workflow/.github/workflows/ci.yml",
    "services/agent/workflow/deploy/chart/Chart.yaml", "services/agent/workflow/deploy/chart/values.yaml",
    "services/agent/model-proxy/package.json", "services/agent/model-proxy/pnpm-lock.yaml", "services/agent/model-proxy/pnpm-workspace.yaml",
    "services/agent/model-proxy/tsconfig.json", "services/agent/model-proxy/biome.json", "services/agent/model-proxy/vitest.config.ts",
    "services/agent/model-proxy/build.mjs", "services/agent/model-proxy/config.yaml", "services/agent/model-proxy/README.md",
    "services/agent/model-proxy/LICENSE", "services/agent/model-proxy/Dockerfile", "services/agent/model-proxy/.dockerignore",
    "services/agent/model-proxy/.github/workflows/ci.yml",
    "services/agent/model-proxy/deploy/chart/Chart.yaml", "services/agent/model-proxy/deploy/chart/values.yaml",
    "services/agent/knowledge/package.json", "services/agent/knowledge/pnpm-lock.yaml", "services/agent/knowledge/pnpm-workspace.yaml",
    "services/agent/knowledge/tsconfig.json", "services/agent/knowledge/biome.json", "services/agent/knowledge/vitest.config.ts",
    "services/agent/knowledge/build.mjs", "services/agent/knowledge/config.yaml", "services/agent/knowledge/README.md",
    "services/agent/knowledge/LICENSE", "services/agent/knowledge/Dockerfile", "services/agent/knowledge/.github/workflows/ci.yml",
    "services/agent/knowledge/forwarder/go.mod", "services/agent/knowledge/forwarder/go.sum",
    "services/agent/knowledge/deploy/chart/Chart.yaml", "services/agent/knowledge/deploy/chart/values.yaml",
    "services/agent/mcp/go.mod", "services/agent/mcp/go.sum", "services/agent/mcp/config.yaml", "services/agent/mcp/sqlc.yaml",
    "services/agent/mcp/README.md", "services/agent/mcp/LICENSE", "services/agent/mcp/Dockerfile", "services/agent/mcp/tools/sqlc.sh",
    "services/agent/mcp/.github/workflows/ci.yml",
    "services/agent/mcp/deploy/chart/Chart.yaml", "services/agent/mcp/deploy/chart/values.yaml",
    "services/agent/background-worker/package.json", "services/agent/background-worker/pnpm-lock.yaml", "services/agent/background-worker/pnpm-workspace.yaml",
    "services/agent/background-worker/tsconfig.json", "services/agent/background-worker/biome.json", "services/agent/background-worker/vitest.config.ts",
    "services/agent/background-worker/build.mjs", "services/agent/background-worker/config.yaml", "services/agent/background-worker/README.md",
    "services/agent/background-worker/Dockerfile", "services/agent/background-worker/.github/workflows/ci.yml",
    "services/agent/background-worker/deploy/chart/Chart.yaml", "services/agent/background-worker/deploy/chart/values.yaml",
    "packages/profile-schemas/apollo-snapshot.schema.json", "packages/profile-schemas/config-generation.schema.json",
    "packages/profile-schemas/fixtures.json", "packages/profile-schemas/python/config_generation.py", "packages/profile-schemas/README.md",
    "deploy/dev/nats-setup.sh", "deploy/dev/nats/anvilkit-knowledge.json", "deploy/dev/nats/anvilkit-mcp.json", "deploy/dev/nats/anvilkit-control.json",
    "deploy/dev/control-chart.sh", "deploy/dev/workflow-chart.sh", "deploy/dev/api-chart.sh", "deploy/dev/model-proxy-chart.sh",
    "deploy/dev/values/anvilkit-agent-api.yaml", "deploy/dev/values/anvilkit-agent-control.yaml", "deploy/dev/values/anvilkit-agent-workflow.yaml",
    "deploy/dev/values/anvilkit-agent-model-proxy.yaml",
    "tools/check-docs.py", "tools/check-contracts.py", "tools/generate-contracts.py",
    "tools/run-verification.py", "tools/check-source-export.py", "tools/pyenv_check.py",
    "tools/requirements.txt", "tools/verification-env.sh",
)
# This P10 directory is generated from checked source by host-bundles.ts;
# it is a build cache, never a required source input or an exported artifact.
GENERATED_OUTPUT_PREFIXES = ("jobs/validator/fixtures/host/browser/dist/", "services/agent/model-proxy/dist/", "jobs/codegen/team/dist/", "services/agent/knowledge/dist/", "services/agent/background-worker/dist/")
EXCLUDED_PREFIXES = ("docs/archive/", "outputs/", "logs/", ".local/", "node_modules/")
EXCLUDED_INFIXES = ("/node_modules/", "/__pycache__/", ".egg-info/")
EXCLUDED_PATTERNS = (re.compile(r"(^|/)\.env(\.|$)"), re.compile(r"\.pem$"), re.compile(r"\.log$"), re.compile(r"\.kubeconfig$"))
LEGACY_TOOLING = ("deploy/local/", "compose.yaml", "tools/prepare-local-compose.py", "tools/component/")


def git(*args: str, cwd: pathlib.Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def listing(repo: pathlib.Path) -> tuple[set[str], set[str]]:
    """Tracked blobs plus untracked non-ignored files of one repository; gitlinks separately."""
    files: set[str] = set()
    gitlinks: set[str] = set()
    for line in git("ls-files", "--stage", cwd=repo).splitlines():
        mode, _, _, path = line.split(None, 3)
        (gitlinks if mode == "160000" else files).add(path)
    files.update(p for p in git("ls-files", "--others", "--exclude-standard", cwd=repo).splitlines() if p)
    # An index entry whose file was deleted in the worktree (an uncommitted removal) is not source.
    files.difference_update(p for p in git("ls-files", "--deleted", cwd=repo).splitlines() if p)
    return files, gitlinks


def export_set() -> tuple[set[str], set[str], set[str]]:
    """The parent's listing plus each mounted replacement repository's own listing.

    Returns (files, legacy submodule paths, replacement repositories found)."""
    files, gitlinks = listing(ROOT)
    found: set[str] = set()
    for rel in REPLACEMENT_REPOS:
        repo = ROOT / rel
        if not (repo / ".git").exists():
            continue  # not mounted: the required-input check reports what is missing
        found.add(rel)
        files.discard(rel + "/")  # an unregistered nested checkout shows up as one untracked directory entry
        gitlinks.discard(rel)
        nested, _ = listing(repo)
        files.update(f"{rel}/{p}" for p in nested)
    return files, gitlinks, found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", help="directory to leave the export in (default: a temporary directory that is removed)")
    ap.add_argument("--skip-build", action="store_true", help="skip go build in the export (docs/contract checks still run)")
    a = ap.parse_args()
    failures: list[str] = []

    files, submodules, repos = export_set()
    for rel in REQUIRED_FILES:
        if rel not in files:
            failures.append(f"required input not in the export set: {rel}")
    for d in REQUIRED_DIRS:
        base = ROOT / d
        if not base.is_dir():
            failures.append(f"required input directory missing: {d}")
            continue
        for path in base.rglob("*"):
            # A mounted repository's .git (a gitdir file or directory) is Git metadata, not an input.
            if path.is_file() and not any(part in ("__pycache__", "node_modules", ".pytest_cache", ".git") or part.endswith(".egg-info") for part in path.parts):
                rel = path.relative_to(ROOT).as_posix()
                if rel.startswith(GENERATED_OUTPUT_PREFIXES):
                    continue
                if rel not in files:
                    failures.append(f"file under a declared input directory is ignored or untracked-ignored: {rel}")
    for rel in sorted(files):
        if rel.startswith(EXCLUDED_PREFIXES + GENERATED_OUTPUT_PREFIXES) or any(i in rel for i in EXCLUDED_INFIXES):
            failures.append(f"excluded path in the export set: {rel}")
        if any(p.search(rel) for p in EXCLUDED_PATTERNS):
            failures.append(f"secret or output pattern in the export set: {rel}")
        if any(rel.startswith(s + "/") for s in submodules):
            failures.append(f"legacy submodule content in the export set: {rel}")
    legacy = sorted(rel for rel in files if rel.startswith(LEGACY_TOOLING))

    print(f"export set: {len(files)} files; replacement repositories included: {', '.join(sorted(repos)) or 'none'}; "
          f"{len(submodules)} legacy submodule pointers excluded: {', '.join(sorted(submodules))}")
    if legacy:
        print(f"retained legacy environment tooling included (cleanup record): {len(legacy)} files under {', '.join(LEGACY_TOOLING)}")
    if failures:
        for f in failures:
            print("FAIL", f)
        print(f"source export: {len(failures)} failures before materialization")
        return 1

    keep = pathlib.Path(a.keep).resolve() if a.keep else None
    tmp = None
    if keep:
        keep.mkdir(parents=True, exist_ok=True)
        target = keep
    else:
        tmp = tempfile.TemporaryDirectory(prefix="anvilkit-export-")
        target = pathlib.Path(tmp.name)
    try:
        for rel in sorted(files):
            src = ROOT / rel
            if not src.is_file():
                continue
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        env = dict(os.environ, GOFLAGS="-mod=readonly")
        steps: list[tuple] = [
            ("docs", [sys.executable, "tools/check-docs.py"], target),
            ("contracts", [sys.executable, "tools/check-contracts.py"], target),
        ]
        if not a.skip_build:
            for mod in re.findall(r"^\s*\./(\S+)", (target / "go.work").read_text(encoding="utf-8"), flags=re.M):
                steps.append((f"go build {mod}", ["go", "build", "-buildvcs=false", "./..."], target / mod))
            # The service repositories also build alone: without the workspace, from their
            # own go.mod against the published contracts module (their independent delivery).
            for repo in ("services/agent/api", "services/agent/control", "services/agent/workflow", "services/agent/mcp", "services/agent/knowledge/forwarder"):
                steps.append((f"go build {repo} (GOWORK=off)", ["go", "build", "-buildvcs=false", "./..."], target / repo, {"GOWORK": "off"}))
        for name, cmd, cwd, *extra in steps:
            p = subprocess.run(cmd, cwd=cwd, env=dict(env, **(extra[0] if extra else {})), capture_output=True, text=True)
            status = "PASS" if p.returncode == 0 else "FAIL"
            print(f"{status:<5} {name}")
            if p.returncode != 0:
                failures.append(f"{name} failed in the export: {(p.stdout + p.stderr).strip()[-1500:]}")
        # The export is self-contained only if nothing from outside it was needed:
        # the checks above ran inside the temporary tree with GOFLAGS=-mod=readonly.
    finally:
        if tmp:
            tmp.cleanup()
    for f in failures:
        print("FAIL", f)
    print(f"source export: {len(failures)} failures" + (f" (export kept at {keep})" if keep else " (disposable export removed)"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
