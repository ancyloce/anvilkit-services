#!/usr/bin/env python3
"""Disposable source-export exercise of the replacement baseline (delivery.md P01 exit).

Usage: python3 tools/check-source-export.py [--keep DIR] [--skip-build] [--self-test]

A `.gitignore` edit is not evidence that the design and build inputs are included in the
source, and neither is a file that merely exists on disk. Since P3.1 (plan 0004, AC5) this
check computes the export set the way a clean recursive clone would see it: the parent's
committed tree at HEAD and, for each replacement repository mounted in the tree (the contracts
repository at contracts/, the API, Control and Workflow repositories at
services/agent/{api,control,workflow,model-proxy}, since P14 the Knowledge and MCP repositories
at services/agent/{knowledge,mcp}, the access sidecar repository at jobs/shared/access-sidecar,
the validator repository at jobs/validator, since 2026-10-04, the Background Worker, Inference
and parser Job repositories at services/agent/{background-worker,inference} and jobs/parser and,
since 2026-10-06, the codegen Job's supervisor and team repositories at
jobs/codegen/{supervisor,team}), that repository's committed tree at the gitlink the parent
records. Untracked files, uncommitted edits and a submodule checked out at another commit are
never part of the export, even when present on disk; they are reported. A replacement
repository the parent does not record as a gitlink is not in a clean clone and fails. The
shared configuration schemas (packages/profile-schemas) are a plain directory of the parent.
Then:

  1. Required inputs: every file under each declared input directory and every anchor file
     (contract sources and generated bindings in contracts/, migrations, the Go modules of the
     replacement, the dev foundation, the current documentation, the verification tools) is in
     the export set.
  2. Exclusions: nothing from docs/archive/, outputs/, logs/, .local/, node_modules/,
     __pycache__/ or *.egg-info/, no `.env*`, `*.pem`, `*.log`, `*.kubeconfig`, and nothing
     from inside a legacy submodule.
  3. Disposable export: the committed trees are extracted (`git archive`) into a temporary
     directory and checked there on its own: tools/check-docs.py, tools/check-contracts.py and `go build ./...`
     in every go.work module (GOFLAGS=-mod=readonly), so the export is self-contained.

Legacy environment tooling that the cleanup record keeps (deploy/local/, compose.yaml,
tools/prepare-local-compose.py, tools/component/) is reported, not required and not failed.
Git is only read; the export lives in a temporary directory unless --keep names one.
--self-test builds a scratch repository in a temporary directory and proves the negative: a
required input present on disk but untracked fails, and an uncommitted edit is not exported.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pyenv_check  # noqa: E402

# The disposable export runs check-contracts.py with this interpreter.
pyenv_check.require("yaml", "jsonschema", "referencing", "openapi_spec_validator")

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Repositories of the replacement mounted inside this tree: their content is source,
# listed through their own Git. Every other gitlink is a submodule without an
# implementation in this closure (excluded).
REPLACEMENT_REPOS = ("contracts", "services/agent/api", "services/agent/control", "services/agent/workflow", "services/agent/model-proxy", "services/agent/knowledge", "services/agent/mcp", "jobs/shared/access-sidecar", "jobs/validator", "services/agent/background-worker", "services/agent/inference", "jobs/parser", "jobs/codegen/supervisor", "jobs/codegen/team")
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
    "jobs/codegen/supervisor/cmd",
    "jobs/codegen/supervisor/internal",
    "jobs/codegen/team/src",
    "jobs/codegen/team/test",
    "jobs/codegen/team/agent/team/prompts",
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
    "services/agent/inference/src",
    "services/agent/inference/tests",
    "services/agent/inference/bin",
    "services/agent/inference/deploy",
    "jobs/parser/src",
    "jobs/parser/tests",
    "jobs/parser/bin",
    "packages/profile-schemas",
    "tests/contracts",
    "tests/integration",
    "tests/evals/rag/corpus",
    "deploy/dev",
    "deploy/handover",
    # P3 (plan 0004): the release, promotion and qualification tooling, the
    # operations runbooks, the load tool and the parent's CI.
    "deploy/gitops",
    "deploy/qualification/platform/observability",
    "deploy/qualification/platform/backups",
    "docs/operations",
    "tests/load",
    ".github/workflows",
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
    "jobs/codegen/README.md",
    "jobs/codegen/supervisor/go.mod", "jobs/codegen/supervisor/go.sum", "jobs/codegen/supervisor/config.yaml",
    "jobs/codegen/supervisor/Dockerfile", "jobs/codegen/supervisor/README.md", "jobs/codegen/supervisor/LICENSE",
    "jobs/codegen/supervisor/agent/resources.json", "jobs/codegen/supervisor/fixtures/fixed-input.txt",
    "jobs/codegen/supervisor/tools/sync-protocol.sh", "jobs/codegen/supervisor/.github/workflows/ci.yml",
    "jobs/codegen/supervisor/internal/protocol/contract/protocol.schema.json",
    "jobs/codegen/supervisor/internal/protocol/contract/fixtures.json", "jobs/codegen/supervisor/internal/protocol/contract/SOURCE",
    "jobs/codegen/team/Dockerfile", "jobs/codegen/team/team.yaml", "jobs/codegen/team/fixtures/brief.json",
    "jobs/codegen/team/agent/team/tools.json", "jobs/codegen/team/LICENSE", "jobs/codegen/team/.github/workflows/ci.yml",
    "jobs/codegen/team/contract/protocol.schema.json", "jobs/codegen/team/contract/fixtures.json", "jobs/codegen/team/contract/SOURCE",
    "jobs/codegen/team/tools/sync-protocol.sh", "jobs/codegen/team/tools/image-smoke.sh",
    "contracts/jobs/codegen/protocol.schema.json", "contracts/jobs/codegen/fixtures.json",
    "jobs/codegen/team/package.json", "jobs/codegen/team/pnpm-lock.yaml", "jobs/codegen/team/pnpm-workspace.yaml",
    "jobs/codegen/team/tsconfig.json", "jobs/codegen/team/tsconfig.build.json", "jobs/codegen/team/biome.json",
    "jobs/codegen/team/vitest.config.ts", "jobs/codegen/team/README.md",
    "jobs/shared/access-sidecar/go.mod", "jobs/shared/access-sidecar/go.sum", "jobs/shared/access-sidecar/config.yaml",
    "jobs/shared/access-sidecar/Dockerfile", "jobs/shared/access-sidecar/README.md", "jobs/shared/access-sidecar/LICENSE",
    "jobs/validator/package.json", "jobs/validator/pnpm-lock.yaml", "jobs/validator/pnpm-workspace.yaml", "jobs/validator/tsconfig.json",
    "jobs/validator/tsconfig.build.json", "jobs/validator/biome.json", "jobs/validator/vitest.config.ts", "jobs/validator/config.yaml",
    "jobs/validator/Dockerfile", "jobs/validator/README.md", "jobs/validator/LICENSE",
    "deploy/policies/chart/Chart.yaml", "deploy/policies/chart/values.yaml", "deploy/policies/chart/templates/components-jobs.yaml",
    "deploy/policies/chart/templates/parsing-jobs.yaml", "deploy/policies/chart/templates/registries.yaml", "deploy/policies/chart/templates/seccomp.yaml",
    "deploy/policies/chart/files/anvilkit-candidate.json", "deploy/policies/seccomp/generate.sh", "deploy/dev/values/anvilkit-job-admission.yaml",
    "deploy/policies/network/anvilkit-components-egress.yaml", "deploy/policies/check.sh", "deploy/policies/README.md",
    "deploy/policies/network/anvilkit-parsing-egress.yaml",
    "deploy/qualification/platform/network/Chart.yaml", "deploy/qualification/platform/network/values.yaml",
    "deploy/qualification/platform/network/templates/policies.yaml", "deploy/qualification/check-network.py",
    "deploy/qualification/platform/secrets/Chart.yaml", "deploy/qualification/platform/secrets/values.yaml",
    "deploy/qualification/platform/secrets/templates/sync.yaml", "deploy/qualification/check-secrets.py",
    "services/agent/inference/tests/fixtures/tls/a.crt", "services/agent/inference/tests/fixtures/tls/a.key",
    "services/agent/inference/tests/fixtures/tls/b.crt", "services/agent/inference/tests/fixtures/tls/b.key",
    "services/agent/inference/tests/fixtures/tls/README",
    "jobs/parser/Dockerfile", "jobs/parser/pyproject.toml", "jobs/parser/requirements.in", "jobs/parser/requirements.lock",
    "jobs/parser/models.lock", "jobs/parser/README.md", "jobs/parser/LICENSE", "jobs/parser/.github/workflows/ci.yml",
    "services/agent/inference/Dockerfile", "services/agent/inference/pyproject.toml", "services/agent/inference/requirements.in",
    "services/agent/inference/requirements.lock", "services/agent/inference/models.lock", "services/agent/inference/config.yaml",
    "services/agent/inference/README.md", "services/agent/inference/LICENSE", "services/agent/inference/.github/workflows/ci.yml",
    "deploy/dev/images.sh", "deploy/dev/docker/access-sidecar.dev.Dockerfile",
    "jobs/migration/internal/migrate/sql/knowledge/00001_init.sql",
    "jobs/migration/internal/migrate/sql/knowledge/00002_background.sql",
    "jobs/migration/internal/migrate/sql/knowledge/00003_ingestion.sql",
    "jobs/migration/internal/migrate/sql/knowledge/00004_retrieval.sql",
    "jobs/migration/internal/migrate/sql/knowledge/00005_memory.sql",
    "jobs/migration/internal/migrate/sql/knowledge/00006_outbox_observation.sql",
    "jobs/migration/internal/migrate/sql/knowledge/00007_memory_removals.sql",
    "tests/evals/rag/cases.json", "tests/evals/rag/README.md",
    "tests/evals/memory/cases.json", "tests/evals/memory/README.md",
    "jobs/migration/internal/migrate/sql/mcp/00001_init.sql",
    "jobs/migration/internal/migrate/sql/mcp/00002_background.sql",
    "jobs/migration/internal/migrate/sql/mcp/00003_catalog_grants.sql", "jobs/migration/internal/migrate/sql/mcp/00004_tool_calls.sql",
    "tests/integration/cmd/mcpfixture/main.go",
    "jobs/validator/src/preview-origin.ts", "jobs/validator/fixtures/preview/frame.tsx",
    "tests/integration/studio_integration_test.go",
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
    "services/agent/control/internal/migrate/sql/00012_preview_builds.sql",
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
    "services/agent/background-worker/Dockerfile", "services/agent/background-worker/.github/workflows/ci.yml", "services/agent/background-worker/LICENSE",
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
GENERATED_OUTPUT_PREFIXES = ("jobs/validator/fixtures/host/browser/dist/", "jobs/validator/fixtures/preview/dist/", "services/agent/model-proxy/dist/", "jobs/codegen/team/dist/", "services/agent/knowledge/dist/", "services/agent/background-worker/dist/")
EXCLUDED_PREFIXES = ("docs/archive/", "outputs/", "logs/", ".local/", "node_modules/")
EXCLUDED_INFIXES = ("/node_modules/", "/__pycache__/", ".egg-info/")
EXCLUDED_PATTERNS = (re.compile(r"(^|/)\.env(\.|$)"), re.compile(r"\.pem$"), re.compile(r"\.log$"), re.compile(r"\.kubeconfig$"))
LEGACY_TOOLING = ("deploy/local/", "compose.yaml", "tools/prepare-local-compose.py", "tools/component/")


def git(*args: str, cwd: pathlib.Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def committed(repo: pathlib.Path, commit: str) -> tuple[set[str], dict[str, str]]:
    """Files of one repository's committed tree; gitlinks (path -> commit) separately."""
    files: set[str] = set()
    gitlinks: dict[str, str] = {}
    out = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", commit], cwd=repo, capture_output=True, check=True).stdout
    for entry in out.decode("utf-8").split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        mode, _, sha = meta.split(" ")
        if mode == "160000":
            gitlinks[path] = sha
        else:
            files.add(path)
    return files, gitlinks


def worktree_drift(repo: pathlib.Path, commit: str) -> list[str]:
    """Untracked-not-ignored files and uncommitted edits that the export leaves out (reported)."""
    drift = [f"untracked {p}" for p in git("ls-files", "--others", "--exclude-standard", cwd=repo).splitlines() if p]
    drift += [f"uncommitted {p}" for p in git("diff", "--name-only", commit, "--", ".", cwd=repo).splitlines() if p]
    return drift


def export_set(root: pathlib.Path = ROOT, repos: tuple[str, ...] = REPLACEMENT_REPOS) -> tuple[set[str], set[str], dict[str, str], list[str], list[str]]:
    """The parent's committed tree plus each replacement repository's tree at its recorded gitlink.

    Returns (files, legacy submodule paths, replacement repositories -> exported commit,
    failures, drift notes)."""
    files, gitlinks = committed(root, "HEAD")
    found: dict[str, str] = {}
    failures: list[str] = []
    drift = [f"parent: {d}" for d in worktree_drift(root, "HEAD")]
    for rel in repos:
        repo = root / rel
        if not (repo / ".git").exists():
            continue  # not mounted: the required-input check reports what is missing
        commit = gitlinks.pop(rel, None)
        if commit is None:
            failures.append(f"replacement repository not recorded as a gitlink at the parent's HEAD (absent from a clean clone): {rel}")
            continue
        try:
            nested, _ = committed(repo, commit)
        except subprocess.CalledProcessError:
            failures.append(f"{rel}: the recorded commit {commit[:12]} is not available in the local repository")
            continue
        found[rel] = commit
        files.update(f"{rel}/{p}" for p in nested)
        head = git("rev-parse", "HEAD", cwd=repo).strip()
        if head != commit:
            drift.append(f"{rel}: checked out at {head[:12]}, the parent records {commit[:12]} (exported)")
        drift += [f"{rel}: {d}" for d in worktree_drift(repo, commit)]
    return files, set(gitlinks), found, failures, drift


def required_failures(root: pathlib.Path, files: set[str], required_files, required_dirs) -> list[str]:
    """Required inputs missing from the export set; files on disk under a declared input
    directory that the export does not contain fail even though they exist (AC5)."""
    failures: list[str] = []
    for rel in required_files:
        if rel not in files:
            failures.append(f"required input not in the export set: {rel}")
    for d in required_dirs:
        base = root / d
        if not base.is_dir():
            failures.append(f"required input directory missing: {d}")
            continue
        for path in base.rglob("*"):
            # A mounted repository's .git (a gitdir file or directory) is Git metadata, not an input.
            if path.is_file() and not any(part in ("__pycache__", "node_modules", ".pytest_cache", ".git") or part.endswith(".egg-info") for part in path.parts):
                rel = path.relative_to(root).as_posix()
                if rel.startswith(GENERATED_OUTPUT_PREFIXES):
                    continue
                if rel not in files:
                    failures.append(f"file under a declared input directory is untracked, uncommitted or ignored: {rel}")
    return failures


def extract(repo: pathlib.Path, commit: str, target: pathlib.Path) -> None:
    """Extract one repository's committed tree (git archive) under target."""
    data = subprocess.run(["git", "archive", "--format=tar", commit], cwd=repo, capture_output=True, check=True).stdout
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(target, filter="tar")


def self_test() -> int:
    """AC5 negative: a required input present on disk but untracked fails; an uncommitted
    edit of a tracked file is not exported."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="anvilkit-export-selftest-") as tmp:
        root = pathlib.Path(tmp)
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        def run(*args: str) -> None:
            subprocess.run(["git", "-c", "user.name=self-test", "-c", "user.email=self-test@invalid", "-c", "commit.gpgsign=false", *args], cwd=root, env=env, check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        (root / "inputs").mkdir()
        (root / "inputs" / "tracked.txt").write_text("committed\n")
        (root / "README.md").write_text("committed\n")
        run("add", "README.md", "inputs/tracked.txt")
        run("commit", "-q", "-m", "self-test")
        (root / "inputs" / "untracked.txt").write_text("on disk only\n")
        (root / "README.md").write_text("uncommitted edit\n")
        files, _, _, fails, drift = export_set(root, ())
        missing = required_failures(root, files, ("README.md", "inputs/untracked.txt"), ("inputs",))
        cases = [
            ("a required file present on disk but untracked fails", any("required input not in the export set: inputs/untracked.txt" in m for m in missing)),
            ("a file on disk under a declared input directory but untracked fails", any("declared input directory is untracked" in m and "inputs/untracked.txt" in m for m in missing)),
            ("a committed required file passes", not any(m.endswith(": README.md") for m in missing) and "inputs/tracked.txt" in files),
            ("the untracked file and the uncommitted edit are reported as drift", "parent: untracked inputs/untracked.txt" in drift and "parent: uncommitted README.md" in drift),
            ("no failure from the listing itself", not fails),
        ]
        out = root / "export"
        extract(root, "HEAD", out)
        cases.append(("the export holds the committed content, not the uncommitted edit", (out / "README.md").read_text() == "committed\n" and not (out / "inputs" / "untracked.txt").exists()))
        for name, ok in cases:
            print(f"{'PASS' if ok else 'FAIL':<5} self-test: {name}")
            if not ok:
                failures.append(name)
    print(f"source export self-test: {len(cases) - len(failures)}/{len(cases)} PASS")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", help="directory to leave the export in (default: a temporary directory that is removed)")
    ap.add_argument("--skip-build", action="store_true", help="skip go build in the export (docs/contract checks still run)")
    ap.add_argument("--self-test", action="store_true", help="prove the untracked-input negative in a scratch repository and exit")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    files, submodules, repos, failures, drift = export_set()
    failures += required_failures(ROOT, files, REQUIRED_FILES, REQUIRED_DIRS)
    for rel in sorted(files):
        if rel.startswith(EXCLUDED_PREFIXES + GENERATED_OUTPUT_PREFIXES) or any(i in rel for i in EXCLUDED_INFIXES):
            failures.append(f"excluded path in the export set: {rel}")
        if any(p.search(rel) for p in EXCLUDED_PATTERNS):
            failures.append(f"secret or output pattern in the export set: {rel}")
        if any(rel.startswith(s + "/") for s in submodules):
            failures.append(f"legacy submodule content in the export set: {rel}")
    legacy = sorted(rel for rel in files if rel.startswith(LEGACY_TOOLING))

    print(f"export set (committed trees): {len(files)} files; replacement repositories included: "
          f"{', '.join(f'{r}@{c[:7]}' for r, c in sorted(repos.items())) or 'none'}; "
          f"{len(submodules)} legacy submodule pointers excluded: {', '.join(sorted(submodules))}")
    if legacy:
        print(f"retained legacy environment tooling included (cleanup record): {len(legacy)} files under {', '.join(LEGACY_TOOLING)}")
    if drift:
        print(f"not exported (working tree differs from the committed source): {len(drift)} entries")
        for d in drift[:40]:
            print(f"  {d}")
        if len(drift) > 40:
            print(f"  ... {len(drift) - 40} more")
    if failures:
        for f in failures[:200]:
            print("FAIL", f)
        if len(failures) > 200:
            print(f"FAIL ... {len(failures) - 200} more")
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
        extract(ROOT, "HEAD", target)
        for rel, commit in sorted(repos.items()):
            extract(ROOT / rel, commit, target / rel)
        for legacy_link in submodules:
            shutil.rmtree(target / legacy_link, ignore_errors=True)
        env = dict(os.environ, GOFLAGS="-mod=readonly")
        # The export has no Git metadata: the generated status blocks are checked
        # against the source repositories' facts (the parent's gitlinks, the
        # contracts' go/v* tags), recorded outside the exported tree.
        meta_dir = tempfile.TemporaryDirectory(prefix="anvilkit-export-meta-")
        meta = pathlib.Path(meta_dir.name) / "docs-status-metadata.json"
        tags: dict[str, list[str]] = {}
        for line in git("for-each-ref", "--format=%(objectname) %(*objectname) %(refname:short)", "refs/tags/go/", cwd=ROOT / "contracts").splitlines():
            parts = line.split()
            tags.setdefault(parts[1] if len(parts) == 3 else parts[0], []).append(parts[-1])
        meta.write_text(json.dumps({"gitlinks": dict(sorted(repos.items())), "contractsTags": tags}))
        docs_env = {"ANVILKIT_DOCS_STATUS_METADATA": str(meta)}
        steps: list[tuple] = [
            ("docs", [sys.executable, "tools/check-docs.py"], target, docs_env),
            ("contracts", [sys.executable, "tools/check-contracts.py"], target),
        ]
        # The codegen Job's repositories carry verbatim copies of the process
        # protocol: the exported copies are the exported contracts' source.
        for repo in ("jobs/codegen/supervisor", "jobs/codegen/team"):
            steps.append((f"codegen protocol copy {repo}", ["sh", "tools/sync-protocol.sh", "--check", str(target / "contracts")], target / repo))
        if not a.skip_build:
            for mod in re.findall(r"^\s*\./(\S+)", (target / "go.work").read_text(encoding="utf-8"), flags=re.M):
                steps.append((f"go build {mod}", ["go", "build", "-buildvcs=false", "./..."], target / mod))
            # The service repositories also build alone: without the workspace, from their
            # own go.mod against the published contracts module (their independent delivery).
            for repo in ("services/agent/api", "services/agent/control", "services/agent/workflow", "services/agent/mcp", "services/agent/knowledge/forwarder", "jobs/codegen/supervisor"):
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
        if "meta_dir" in locals():
            meta_dir.cleanup()
    for f in failures:
        print("FAIL", f)
    print(f"source export: {len(failures)} failures" + (f" (export kept at {keep})" if keep else " (disposable export removed)"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
