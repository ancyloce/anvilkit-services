#!/usr/bin/env python3
"""P24a: the handover rehearsal of the replacement release (DEVELOPMENT_ONLY placement).

  PY=.local/verification-venv/bin/python
  $PY deploy/handover/rehearse.py inputs  --run RUN --release REL
  $PY deploy/handover/rehearse.py install --run RUN --release REL --reset-qualification-environment
  $PY deploy/handover/rehearse.py install-check --run RUN --release REL
  $PY deploy/handover/rehearse.py closure --run RUN --release REL
  $PY deploy/handover/rehearse.py dto     --run RUN
  $PY deploy/handover/rehearse.py legacy  --run RUN
  $PY deploy/handover/rehearse.py withdraw --run RUN --release REL
  $PY deploy/handover/rehearse.py record  --run RUN --release REL

REL is a release run of tools/release-artifacts.py (outputs/qualification/<REL>/release/
release-manifest.json) whose environment chart deploy/gitops/publish.py published; RUN
names this rehearsal's evidence directory outputs/handover/<RUN>/ (ignored by Git).

  inputs         freezes the identities the rehearsal binds: the release manifest and its
                 environment chart, P22's release-candidate inventory, the latest P23 record,
                 the legacy anvilkit-local environment (Compose project, containers, images,
                 volumes, the files that define and build it, credential file names and
                 certificate expiry only), the legacy API contract and its request examples,
                 and the Studio page store (pages and remote-component locks, read-only).
  install        a clean installation from zero on the qualification environment
                 (deploy/qualification): the previous environment's state is moved aside
                 under .local/qualification-archive/<RUN>/ (never deleted), its cluster and DR
                 store are removed, then cluster.sh up and bootstrap.py install REL on empty
                 state. Refuses without --reset-qualification-environment.
  install-check  proves the installation started from zero: every database cluster was
                 bootstrapped by initdb inside the install window, every owned schema's
                 migrations 00001..N were applied in it, the Temporal namespace holds no
                 execution from before it, no volume or container belongs to the legacy
                 environment, every anvilkit workload runs a release image, the probes passed.
  closure        the new build closure of the release images: Go build information and
                 package paths of every Go binary, the SBOM of every image, checked against
                 the legacy identities (tests/contracts/closure_test.go's list); the legacy
                 images are the positive control and must be flagged.
  dto            old-DTO separation and the route inventory against the installed API:
                 every legacy route and request example (docs/archive/contracts/openapi) is
                 refused, nothing is created, every new route is served.
  legacy         the read-only inventory of the legacy records and in-flight obligations,
                 on a disposable copy of its volumes (PostgreSQL 18.6 on an isolated network).
  withdraw       the withdrawal rehearsal: before business writes the release is withdrawn
                 (entry closed, environment removed) and nothing of it stays active; after
                 business writes admission is closed and every obligation reconciled before
                 the entry reopens (forward repair).
  record         binds every piece of evidence above into handover-rehearsal.{json,md}; G-13
                 stays NOT_RUN unless its entire real-environment evidence exists.

Exit 0 when the step's checks passed (record: when the record was written), 1 when a
check failed, 2 when an input is missing or a tool is unavailable.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "tools"))
import pyenv_check  # noqa: E402

pyenv_check.require("yaml")
import yaml  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = str(ROOT / ".local/verification-venv/bin/python")
OUT = ROOT / "outputs/handover"
QUAL = ROOT / "outputs/qualification"
STUDIO = pathlib.Path(os.environ.get("ANVILKIT_STUDIO_DIR") or ROOT.parent / "anvilkit-studio")
BUSYBOX = "busybox:1.37.0"
LEGACY_PROJECT = "anvilkit-local"
# What defines, builds or feeds the legacy environment (delivery.md, cleanup record).
LEGACY_FILES = ["compose.yaml", "deploy/local", "tools/prepare-local-compose.py", "tools/component/hero-fixture.mjs"]
LEGACY_SNAPSHOTS = ".local/legacy"
LEGACY_CREDENTIALS = ".local/compose"
LEGACY_CONTRACTS = "docs/archive/contracts"
LEGACY_OPENAPI = "docs/archive/contracts/openapi/agent-api-v1.openapi.json"
LEGACY_EXAMPLES = "docs/archive/contracts/openapi/agent-api-v1.examples.json"
P22_INVENTORY_GLOB = "outputs/evaluations/*/inventory.json"


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha(path: pathlib.Path) -> str:
    return sha_bytes(path.read_bytes())


def rel(path: pathlib.Path) -> str:
    return str(path.relative_to(ROOT))


def tree_digest(base: pathlib.Path) -> dict:
    """Files, bytes and one digest over the sorted (path, sha256) list of a directory."""
    entries = []
    size = 0
    for p in sorted(x for x in base.rglob("*") if x.is_file() and not x.is_symlink()):
        data = p.read_bytes()
        size += len(data)
        entries.append(f"{p.relative_to(base)}\0{hashlib.sha256(data).hexdigest()}")
    return {"files": len(entries), "bytes": size, "digest": sha_bytes("\n".join(entries).encode())}


def run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def evidence_dir(run_id: str) -> pathlib.Path:
    d = OUT / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(run_id: str, name: str, doc: dict) -> pathlib.Path:
    p = evidence_dir(run_id) / name
    p.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    return p


def release_manifest(rel_run: str) -> tuple[dict, pathlib.Path]:
    p = QUAL / rel_run / "release/release-manifest.json"
    if not p.exists():
        raise SystemExit(f"UNEXECUTED: {rel(p)} is missing")
    return json.loads(p.read_text()), p


# ---------------------------------------------------------------- inputs (P24-01)

def docker_json(*args: str) -> list | dict:
    return json.loads(run(["docker", *args]).stdout)


def legacy_environment() -> dict:
    containers = []
    ids = run(["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={LEGACY_PROJECT}"]).stdout.split()
    for c in docker_json("inspect", *ids) if ids else []:
        containers.append({"name": c["Name"].lstrip("/"), "id": c["Id"][:12], "image": c["Config"]["Image"], "imageId": c["Image"][:19],
                           "service": c["Config"]["Labels"].get("com.docker.compose.service"),
                           "state": c["State"]["Status"], "exitCode": c["State"]["ExitCode"], "finishedAt": c["State"]["FinishedAt"],
                           "restartPolicy": c["HostConfig"]["RestartPolicy"]["Name"], "networkMode": c["HostConfig"]["NetworkMode"],
                           "mounts": [{"type": m["Type"], "source": m.get("Name") or m["Source"].replace(str(ROOT) + "/", ""),
                                       "destination": m["Destination"], "readOnly": not m["RW"]} for m in c["Mounts"]]})
    images = []
    for line in run(["docker", "images", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}"]).stdout.splitlines():
        ref, image_id, created = line.split("\t")
        if ref.startswith(f"{LEGACY_PROJECT}-"):
            images.append({"reference": ref, "id": image_id, "created": created})
    volumes = []
    for name in run(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={LEGACY_PROJECT}"]).stdout.split():
        v = docker_json("volume", "inspect", name)[0]
        # Sizes through a read-only mount on no network; nothing is written to the volume.
        du = run(["docker", "run", "--rm", "--network", "none", "-v", f"{name}:/v:ro", BUSYBOX, "sh", "-c",
                  "du -sb /v | cut -f1; find /v -type f | wc -l"]).stdout.split()
        volumes.append({"name": name, "created": v["CreatedAt"], "bytes": int(du[0]), "files": int(du[1])})
    files = {}
    for f in LEGACY_FILES:
        p = ROOT / f
        if p.is_dir():
            files[f] = tree_digest(p) | {"tracked": bool(run(["git", "ls-files", f], cwd=ROOT).stdout.strip())}
        elif p.exists():
            files[f] = {"sha256": sha(p), "tracked": bool(run(["git", "ls-files", f], cwd=ROOT).stdout.strip())}
        else:
            files[f] = {"absent": True}
    snapshots = {}
    for d in sorted((ROOT / LEGACY_SNAPSHOTS).iterdir()) if (ROOT / LEGACY_SNAPSHOTS).exists() else []:
        manifest = d / "MANIFEST.sha256"
        snapshots[d.name] = {"head": (d / "HEAD").read_text().strip() if (d / "HEAD").exists() else None,
                             "manifest": sha(manifest) if manifest.exists() else None,
                             "worktree": tree_digest(d / "worktree") if (d / "worktree").exists() else None}
    cred_dir = ROOT / LEGACY_CREDENTIALS
    credentials = {"files": sorted(rel(p) for p in cred_dir.rglob("*") if p.is_file()) if cred_dir.exists() else []}
    expiry = set()
    for crt in cred_dir.rglob("*.crt") if cred_dir.exists() else []:
        out = run(["openssl", "x509", "-in", str(crt), "-noout", "-enddate"], check=False).stdout.strip()
        if out.startswith("notAfter="):
            expiry.add(out.split("=", 1)[1])
    credentials["certificateNotAfter"] = sorted(expiry)
    return {"composeProject": LEGACY_PROJECT, "containers": containers, "images": images, "volumes": volumes,
            "definitionFiles": files, "legacySnapshots": snapshots, "credentials": credentials,
            "archivedContracts": tree_digest(ROOT / LEGACY_CONTRACTS)}


def legacy_contract() -> dict:
    spec = json.loads((ROOT / LEGACY_OPENAPI).read_text())
    examples = json.loads((ROOT / LEGACY_EXAMPLES).read_text())
    routes = [f"{m.upper()} {spec['servers'][0]['url'].split('}', 1)[1]}{p}" for p, ops in spec["paths"].items()
              for m in ops if m in ("get", "post", "put", "delete", "patch") and p not in ("/healthz", "/readyz")]
    return {"openapi": {"path": LEGACY_OPENAPI, "sha256": sha(ROOT / LEGACY_OPENAPI)},
            "examples": {"path": LEGACY_EXAMPLES, "sha256": sha(ROOT / LEGACY_EXAMPLES),
                         "requests": sorted(k for k in examples if k.endswith(".request"))},
            "routes": routes}


def studio_pages() -> dict:
    """The Studio's durable page store, opened read-only: pages, and pages whose root
    props carry a remote-component lock (root.props.remoteComponentLock)."""
    db = STUDIO / "apps/studio/.anvilkit/pages.sqlite"
    if not db.exists():
        return {"status": "NOT_FOUND", "path": str(db)}
    head = run(["git", "-C", str(STUDIO), "rev-parse", "HEAD"], check=False).stdout.strip()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, slug, status, updated_at, data FROM pages ORDER BY id").fetchall()
    finally:
        con.close()
    pages, locked = [], []
    for page_id, slug, status, updated, data in rows:
        record = json.loads(data)
        payload = record.get("data") if isinstance(record, dict) and "data" in record else record
        root_props = ((payload or {}).get("root") or {}).get("props") or {}
        has_lock = "remoteComponentLock" in root_props
        pages.append({"id": page_id, "slug": slug, "status": status, "updatedAt": updated, "sha256": sha_bytes(data.encode()), "lock": has_lock})
        if has_lock:
            locked.append(page_id)
    return {"path": str(db), "studioHead": head, "pages": len(pages), "pagesWithRemoteComponentLock": locked, "rows": pages}


def latest(pattern: str) -> pathlib.Path | None:
    found = sorted(ROOT.glob(pattern), key=lambda p: p.parent.name)
    return found[-1] if found else None


def cmd_inputs(a: argparse.Namespace) -> int:
    manifest, mpath = release_manifest(a.release)
    env_manifest = QUAL / a.release / "gitops/environment-manifest.json"
    root_app = QUAL / a.release / "gitops/root-application.yaml"
    inventory = latest(P22_INVENTORY_GLOB)
    records = sorted(QUAL.glob("*/qualification-record.json"), key=lambda p: p.parent.name)
    record = records[-1] if records else None
    doc = {
        "schemaVersion": 1, "step": "inputs", "run": a.run, "recordedAt": now(), "placement": "DEVELOPMENT_ONLY",
        "release": {"run": a.release, "manifest": {"path": rel(mpath), "sha256": sha(mpath)}, "status": manifest["status"],
                    "findings": manifest.get("findings", []), "derivedFrom": manifest.get("derivedFrom"),
                    "images": {k: v["digest"] for k, v in manifest["images"].items()},
                    "charts": {k: {"version": v["version"], "digest": v["digest"]} for k, v in manifest["charts"].items()},
                    "sourcesClean": {k: v["source"].get("clean") for k, v in manifest["images"].items() if isinstance(v.get("source"), dict)},
                    "environment": ({"path": rel(env_manifest), "sha256": sha(env_manifest),
                                     "chart": json.loads(env_manifest.read_text())["environmentChart"]} if env_manifest.exists() else None),
                    "rootApplication": {"path": rel(root_app), "sha256": sha(root_app)} if root_app.exists() else None},
        "p22Inventory": {"path": rel(inventory), "sha256": sha(inventory)} if inventory else None,
        "p23Record": ({"path": rel(record), "sha256": sha(record), "status": json.loads(record.read_text()).get("status")} if record else None),
        "legacyEnvironment": legacy_environment(),
        "legacyContract": legacy_contract(),
        "studioPages": studio_pages(),
    }
    out = write(a.run, "inputs.json", doc)
    le = doc["legacyEnvironment"]
    print(f"release {a.release}: {doc['release']['status']}, {len(doc['release']['images'])} images, {len(doc['release']['charts'])} charts")
    print(f"legacy {LEGACY_PROJECT}: {len(le['containers'])} containers ({', '.join(sorted({c['state'] for c in le['containers']}))}), "
          f"{len(le['images'])} images, {len(le['volumes'])} volumes, certificates expire {', '.join(le['credentials']['certificateNotAfter'])}")
    print(f"legacy contract: {len(doc['legacyContract']['routes'])} routes, {len(doc['legacyContract']['examples']['requests'])} request examples")
    sp = doc["studioPages"]
    print(f"studio pages: {sp.get('pages')} pages, {len(sp.get('pagesWithRemoteComponentLock', []))} with a remote-component lock")
    print(f"written {rel(out)}")
    return 0


# ---------------------------------------------------------------- install (P24-02)

QSTATE = ROOT / ".local/qualification"
# The environment's own state: generated credentials, OpenBao shares, the DR store's
# data and credentials, the kubeconfig. Caches of upstream content (mirror*/, charts/)
# carry no environment state and stay.
QSTATE_MOVED = ["credentials.json", "openbao", "dr.env", "dr", "kubeconfig", "argocd-values.yaml"]
DR_CONTAINER = "anvilkit-qualification-dr"


def cmd_install(a: argparse.Namespace) -> int:
    if not a.reset_qualification_environment:
        print("refused: a clean installation removes the qualification environment's cluster and DR store "
              "(its state is moved under .local/qualification-archive/<RUN>/); pass --reset-qualification-environment")
        return 2
    release_manifest(a.release)
    root_app = QUAL / a.release / "gitops/root-application.yaml"
    if not root_app.exists():
        print(f"UNEXECUTED: {rel(root_app)} is missing: deploy/gitops/publish.py --environment qualification --run {a.release}")
        return 2
    doc = {"schemaVersion": 1, "step": "install", "run": a.run, "release": a.release, "placement": "DEVELOPMENT_ONLY",
           "rootApplication": {"path": rel(root_app), "sha256": sha(root_app)}}
    started = now()
    archive = ROOT / ".local/qualification-archive" / f"{a.run}-{started.replace(':', '').replace('-', '')}"
    doc["resetStartedAt"] = started
    # An earlier attempt of this run keeps its evidence under its own start time.
    earlier = evidence_dir(a.run) / "install.json"
    if earlier.exists():
        tag = json.loads(earlier.read_text()).get("resetStartedAt", "earlier").replace(":", "").replace("-", "")
        for name in ("install.json", "cluster-up.log", "bootstrap.log"):
            if (evidence_dir(a.run) / name).exists():
                (evidence_dir(a.run) / name).rename(evidence_dir(a.run) / f"{name}.attempt-{tag}")
        doc["earlierAttempt"] = f"install.json.attempt-{tag}"
    # 1. Remove the previous environment by exact name: its kind cluster, then its DR store container.
    run(["sh", str(ROOT / "deploy/qualification/cluster.sh"), "down"])
    run(["docker", "rm", "-f", DR_CONTAINER], check=False)
    # 2. Move its state aside, never delete it.
    archive.mkdir(parents=True, exist_ok=False)
    os.chmod(archive.parent, 0o700)
    moved = []
    for name in QSTATE_MOVED:
        src = QSTATE / name
        if src.exists():
            shutil.move(str(src), str(archive / name))
            moved.append(name)
    doc["previousStateArchived"] = {"path": rel(archive), "entries": moved}
    # 3. A new cluster and an empty DR store with new credentials, then the release from zero.
    doc["installStartedAt"] = now()
    up = run(["sh", str(ROOT / "deploy/qualification/cluster.sh"), "up"], check=False)
    (evidence_dir(a.run) / "cluster-up.log").write_text(up.stdout + up.stderr)
    if up.returncode != 0:
        doc["result"] = "FAIL"
        doc["failure"] = "cluster.sh up failed (cluster-up.log)"
        write(a.run, "install.json", doc)
        print(doc["failure"])
        return 1
    log = evidence_dir(a.run) / "bootstrap.log"
    with log.open("w") as f:
        boot = subprocess.run([PY, str(ROOT / "deploy/qualification/bootstrap.py"), "--run", a.release, "--wait", str(a.wait)],
                              stdout=f, stderr=subprocess.STDOUT, text=True)
    doc["installEndedAt"] = now()
    doc["bootstrapExit"] = boot.returncode
    doc["result"] = "PASS" if boot.returncode == 0 else "FAIL"
    write(a.run, "install.json", doc)
    print(f"install {doc['result']}: cluster up, bootstrap exit {boot.returncode} ({rel(log)}); previous state in {rel(archive)}")
    return 0 if boot.returncode == 0 else 1


# ---------------------------------------------------------------- closure (P24-03)

CLOSURE_TEST = ROOT / "tests/contracts/closure_test.go"
NEW_GO_MODULES = ["services/agent/api", "services/agent/control", "services/agent/workflow", "services/agent/mcp",
                  "services/agent/knowledge/forwarder", "jobs/codegen/supervisor", "jobs/migration", "jobs/shared/access-sidecar"]
# The new source trees whose content must share no file with the legacy sources.
NEW_SOURCE_TREES = ["services/agent", "jobs", "contracts", "packages", "tests", "deploy", "tools"]
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", ".pytest_cache", ".turbo", "dist", ".next", "outputs", ".local"}
GO_BUILDINFO = b"\xff Go buildinf:"
LEGACY_IMAGE_IDS = {"anvilkit-local-anvilkit-agent-api:legacy-20260915", "anvilkit-local-anvilkit-agent-control:legacy-20260916",
                    "anvilkit-local-anvilkit-agent-workflow:legacy-20260916"}


def go_list_strings(var: str) -> list[str]:
    """One string slice of tests/contracts/closure_test.go, the one authority of the
    legacy identities (retiredModules, forbiddenPackages)."""
    import re
    block = re.search(var + r" = \[\]string\{(.*?)\n\t\}", CLOSURE_TEST.read_text(), re.S)
    return re.findall(r'"([^"]+)"', block.group(1))


def is_license(path: str) -> bool:
    name = pathlib.PurePosixPath(path).name.upper()
    return name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE"))


def is_git_plumbing(path: str) -> bool:
    """A submodule's `.git` pointer file (`gitdir: …`) and the snapshot's preserved copy of it:
    the same submodule path yields the same bytes, which says nothing about implementation."""
    return pathlib.PurePosixPath(path).name in (".git", ".git.gitdir-file")


def walk_files(base: pathlib.Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            p = pathlib.Path(dirpath) / f
            if p.is_file() and not p.is_symlink():
                yield p


def legacy_sources() -> tuple[dict[str, str], set[str]]:
    """sha256 -> one legacy path, over the preserved legacy worktrees; and the legacy
    Go package import paths (module path + directory of each package)."""
    hashes, packages = {}, set()
    for wt in sorted((ROOT / LEGACY_SNAPSHOTS).glob("*/worktree")):
        module = next(l.split()[1] for l in (wt / "go.mod").read_text().splitlines() if l.startswith("module "))
        for p in walk_files(wt):
            r = str(p.relative_to(wt))
            if not is_license(r) and not is_git_plumbing(r) and p.stat().st_size > 0:
                hashes.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), f"{wt.parent.name}/{r}")
            if p.suffix == ".go":
                d = str(p.parent.relative_to(wt))
                packages.add(module if d == "." else f"{module}/{d}")
    return hashes, packages


def new_packages() -> set[str]:
    out = set()
    for m in NEW_GO_MODULES:
        p = run(["go", "list", "./..."], cwd=ROOT / m, check=False)
        out.update(x for x in p.stdout.split() if x)
    return out


def scan_image(ref: str, legacy_hashes: dict[str, str], markers: dict[str, list[str]], forbidden_modules: list[str]) -> dict:
    """Every regular file of the image's filesystem (docker export, streamed): its
    sha256 against the legacy files, and for each Go binary its build information
    (go version -m) and the legacy package markers in its symbol table."""
    import io
    import tarfile
    import tempfile
    cid = run(["docker", "create", ref]).stdout.strip()
    result = {"files": 0, "bytes": 0, "goBinaries": [], "legacyFileMatches": [], "findings": []}
    try:
        proc = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                h = hashlib.sha256()
                head = b""
                go_data = None
                first = True
                buf = io.BytesIO() if member.size < 512 * 1024 * 1024 else None
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    if first:
                        head = chunk[:4]
                        first = False
                        if head != b"\x7fELF":
                            buf = None
                    h.update(chunk)
                    if buf is not None:
                        buf.write(chunk)
                result["files"] += 1
                result["bytes"] += member.size
                digest = h.hexdigest()
                if digest in legacy_hashes:
                    result["legacyFileMatches"].append({"path": member.name, "legacy": legacy_hashes[digest]})
                if buf is not None:
                    data = buf.getvalue()
                    if GO_BUILDINFO in data:
                        go_data = data
                if go_data is None:
                    continue
                with tempfile.NamedTemporaryFile() as tmp:
                    tmp.write(go_data)
                    tmp.flush()
                    info = run(["go", "version", "-m", tmp.name], check=False).stdout
                main_mod, deps = None, []
                for line in info.splitlines():
                    cols = line.split()
                    if len(cols) >= 2 and cols[0] == "mod":
                        main_mod = cols[1]
                    elif len(cols) >= 2 and cols[0] in ("dep", "=>"):
                        deps.append(cols[1])
                # Symbol names are "<import path>.<name>": a forbidden package matches as a
                # prefix (itself or a package below it), a legacy-only package exactly.
                found = sorted({m for m in markers["prefix"] if (m + ".").encode() in go_data or (m + "/").encode() in go_data}
                               | {m for m in markers["exact"] if (m + ".").encode() in go_data})
                bad_deps = sorted({d for d in deps for fm in forbidden_modules if d == fm or d.startswith(fm + "/")})
                entry = {"path": member.name, "sha256": "sha256:" + digest, "mainModule": main_mod, "dependencies": len(deps),
                         "forbiddenModules": bad_deps, "legacyPackages": found}
                result["goBinaries"].append(entry)
                if bad_deps or found:
                    result["findings"].append(f"{member.name}: forbidden modules {bad_deps}, legacy packages {found}")
        proc.wait()
    finally:
        run(["docker", "rm", "-f", cid], check=False)
    for m in result["legacyFileMatches"]:
        result["findings"].append(f"{m['path']} is byte-identical to legacy {m['legacy']}")
    return result


def cmd_closure(a: argparse.Namespace) -> int:
    manifest, mpath = release_manifest(a.release)
    retired = go_list_strings("retiredModules")
    forbidden = go_list_strings("forbiddenPackages")
    legacy_hashes, legacy_pkgs = legacy_sources()
    new_pkgs = new_packages()
    legacy_only = sorted(legacy_pkgs - new_pkgs)
    # The package markers searched in binaries: the closure test's forbidden packages
    # and every legacy-only package path (a path the new modules also have is a name,
    # not legacy code; source identity is checked by content below).
    markers = {"prefix": forbidden, "exact": legacy_only}
    forbidden_modules = sorted(set(retired) | {x for x in forbidden if "." in x.split("/")[0]})
    doc = {"schemaVersion": 1, "step": "closure", "run": a.run, "release": a.release, "recordedAt": now(),
           "releaseManifest": {"path": rel(mpath), "sha256": sha(mpath)},
           "legacyIdentities": {"source": rel(CLOSURE_TEST), "retiredModules": retired, "forbiddenPackages": forbidden,
                                "legacyOnlyPackages": legacy_only, "legacySourceFiles": len(legacy_hashes),
                                "sharedPackageNames": sorted(legacy_pkgs & new_pkgs)}}
    checks = {}
    # A. Source module closure: the parent's existing build-closure test.
    t = run(["go", "test", "-count=1", "./..."], cwd=ROOT / "tests/contracts", check=False)
    doc["moduleClosure"] = {"command": "go test -count=1 ./... (tests/contracts)", "exit": t.returncode, "output": (t.stdout + t.stderr)[-2000:]}
    checks["moduleClosure"] = t.returncode == 0
    # B. Source content: no legacy file (license texts aside) in the new source trees.
    copies = []
    scanned = 0
    for tree in NEW_SOURCE_TREES:
        for p in walk_files(ROOT / tree):
            scanned += 1
            r = rel(p)
            if is_license(r) or is_git_plumbing(r) or p.stat().st_size == 0:
                continue
            d = hashlib.sha256(p.read_bytes()).hexdigest()
            if d in legacy_hashes:
                copies.append({"path": r, "legacy": legacy_hashes[d]})
    doc["sourceContent"] = {"filesScanned": scanned, "copiesOfLegacyFiles": copies,
                            "excluded": "license texts, empty files and Git submodule pointer files (.git, .git.gitdir-file)"}
    checks["sourceContent"] = not copies
    # C. Image content, release images; D. their SBOMs.
    legacy_binaries: dict[str, str] = {}
    controls = {}
    for ref in sorted(LEGACY_IMAGE_IDS):
        r = scan_image(ref, {}, markers, forbidden_modules)
        for b in r["goBinaries"]:
            legacy_binaries[b["sha256"].split(":", 1)[1]] = f"{ref}:{b['path']}"
        controls[ref] = {"goBinaries": r["goBinaries"], "flagged": bool(r["findings"]), "findings": r["findings"][:10]}
        print(f"control {ref}: {'flagged' if r['findings'] else 'NOT flagged'} ({len(r['goBinaries'])} Go binaries)", flush=True)
    doc["positiveControls"] = controls
    checks["positiveControlsFlagged"] = bool(controls) and all(c["flagged"] for c in controls.values())
    all_legacy = {**legacy_hashes, **legacy_binaries}
    images = {}
    for name, img in sorted(manifest["images"].items()):
        ref = img["reference"].replace("anvilkit-dev-registry:5000", "localhost:5001") if "anvilkit-dev-registry" in img["reference"] else img["reference"]
        pulled = run(["docker", "pull", "-q", ref], check=False)
        if pulled.returncode != 0:
            images[name] = {"status": "UNEXECUTED", "reason": pulled.stderr.strip()[-200:]}
            continue
        r = scan_image(ref, all_legacy, markers, forbidden_modules)
        sbom_entry = img.get("sbom") or {}
        sbom_path = QUAL / img.get("evidenceRun", a.release) / "release" / sbom_entry.get("file", "")
        sbom_findings = []
        if sbom_entry.get("file") and sbom_path.is_file():
            if sha(sbom_path) != sbom_entry.get("digest"):
                sbom_findings.append("SBOM digest differs from the release manifest")
            comps = json.loads(sbom_path.read_text()).get("components", [])
            for c in comps:
                ident = (c.get("purl") or "") + " " + (c.get("name") or "")
                for fm in forbidden_modules:
                    if fm in ident:
                        sbom_findings.append(f"SBOM component {c.get('name')} matches {fm}")
            sbom = {"path": rel(sbom_path), "components": len(comps), "findings": sbom_findings}
        else:
            sbom = {"status": "NOT_FOUND"}
            sbom_findings.append("SBOM missing")
        images[name] = {"digest": img["digest"], "files": r["files"], "bytes": r["bytes"], "goBinaries": r["goBinaries"],
                        "legacyFileMatches": r["legacyFileMatches"], "sbom": sbom, "findings": r["findings"] + sbom_findings,
                        "result": "PASS" if not (r["findings"] or sbom_findings) else "FAIL"}
        print(f"{name}: {images[name]['result']} ({r['files']} files, {len(r['goBinaries'])} Go binaries)", flush=True)
    doc["images"] = images
    checks["imagesFreeOfLegacy"] = all(v.get("result") == "PASS" for v in images.values())
    doc["checks"] = checks
    doc["result"] = "PASS" if all(checks.values()) else "FAIL"
    out = write(a.run, "closure.json", doc)
    print(f"closure {doc['result']}: {json.dumps(checks)} ({rel(out)})")
    return 0 if doc["result"] == "PASS" else 1


# ---------------------------------------------------------------- legacy (P24-05)

LEGACY_PG_IMAGE = "postgres@sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"  # 18.6-alpine, as compose.yaml pins it
LEGACY_VOLUMES = {"postgres": "anvilkit-local_postgres-data", "intake": "anvilkit-local_intake-data", "artifacts": "anvilkit-local_artifact-data"}
# Columns whose values are enumerations or identities, never content: their value
# distribution is recorded; every other column only enters the table digest.
ENUM_COLUMNS = {"kind", "status", "state", "lifecycle", "phase", "outcome", "class", "fixture_id", "intake_source", "effect_kind",
                "disposition", "verdict", "result", "relay_state", "cleanup", "reason_code", "terminal", "family", "action",
                # the legacy operation lifecycle, its money and its Temporal binding (archived control-v1.sql)
                "public_status", "business_stage", "control_state", "cleanup_state", "financial_state", "change_state",
                "funding_state", "intended_terminal_outcome", "expiry_reason", "cancel_requested", "currency",
                "reserved_amount", "confirmed_amount", "exposure_amount", "quoted_credits", "start_state", "resolved",
                "accepted_terminal_type", "temporal_namespace", "delivery_state"}
IDENTITY_COLUMNS = {"tenant_id", "caller_id", "actor_id", "principal_id", "project_id"}
TEMPORAL_STATUS = {1: "Running", 2: "Completed", 3: "Failed", 4: "Canceled", 5: "Terminated", 6: "ContinuedAsNew", 7: "TimedOut"}


def psql(container: str, db: str, sql: str) -> list[list[str]]:
    p = run(["docker", "exec", "-i", container, "psql", "-U", "postgres", "-d", db, "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-f", "-"],
            input=sql)
    return [line.split("\t") for line in p.stdout.splitlines() if line]


def ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def volume_files(volume: str) -> dict:
    script = 'cd /v && find . -type f -exec sh -c \'for f; do printf "%s %s %s\\n" "$(stat -c %s "$f")" "$(sha256sum "$f" | cut -d" " -f1)" "$f"; done\' _ {} + | sort -k3'
    out = run(["docker", "run", "--rm", "--network", "none", "-v", f"{volume}:/v:ro", BUSYBOX, "sh", "-c", script]).stdout
    files = []
    for line in out.splitlines():
        size, digest, path = line.split(" ", 2)
        files.append({"path": path[2:], "bytes": int(size), "sha256": "sha256:" + digest})
    return {"volume": volume, "files": files, "digest": sha_bytes("\n".join(f"{f['path']}\0{f['sha256']}" for f in files).encode())}


# Legacy tables whose rows would be costs, external effects, sends, permits,
# publications or receipts that a handover must keep meaning for (control-v1.sql).
LEGACY_EFFECT_TABLES = ["agent_control.cost_entries", "agent_control.effects", "agent_control.model_calls", "agent_control.permits",
                        "agent_control.release_manifests", "agent_control.test_credit_entries", "agent_control.test_credit_balances",
                        "agent_control.period_corrections", "agent_control.physical_instances", "agent_control.queue_entries"]
LEGACY_TERMINAL = {"succeeded", "failed", "canceled", "expired"}


def assess(doc: dict, inputs: dict | None) -> dict:
    """P24-06: whether the legacy records or obligations need preservation, decided
    by explicit rules over the inventory (delivery.md, conditional business-data
    transfer). The business owner's confirmation stays a pending input."""
    agent = doc["databases"].get("anvilkit_agent", {}).get("tables", {})
    ops = agent.get("agent_control.operations", {})
    status = (ops.get("values") or {}).get("public_status", {})
    preps = (agent.get("agent_control.preparations", {}).get("values") or {}).get("resolved", {})
    amounts = {c: (ops.get("values") or {}).get(c, {}) for c in ("reserved_amount", "confirmed_amount", "exposure_amount", "quoted_credits")}
    open_business = [x for x in doc.get("temporal", {}).get("open", []) if not x["workflowType"].startswith("temporal-sys-")]
    identities = {v for t in agent.values() for c in IDENTITY_COLUMNS for v in ((t.get("values") or {}).get(c) or {})}
    locks = (inputs or {}).get("studioPages", {}).get("pagesWithRemoteComponentLock")
    rules = {
        "everyOperationTerminal": ops.get("rows", 0) == sum(n for k, n in status.items() if k in LEGACY_TERMINAL),
        "everyPreparationResolved": set(preps) <= {"true"},
        "noOpenBusinessExecution": not open_business,
        "noCostEffectSendPermitPublicationOrReceipt": all(agent.get(t, {}).get("rows", 0) == 0 for t in LEGACY_EFFECT_TABLES),
        "everyAmountZero": all(set(v) <= {"0", "<null>"} for v in amounts.values()),
        "onlyDevelopmentFixturePrincipals": bool(identities) and all(i.startswith("fixture-") for i in identities),
        "noStudioPageLocksLegacyArtifacts": locks == [],
    }
    applies = not all(rules.values())
    return {"rules": rules, "operations": {"rows": ops.get("rows", 0), "publicStatus": status},
            "identities": sorted(identities), "openBusinessExecutions": open_business,
            "transferRequired": applies,
            "transferRehearsal": "REQUIRED" if applies else "INAPPLICABLE",
            "decision": ("records or obligations need preservation: rehearse the import" if applies else
                         "no business record, cost, receipt, publication, page lock or in-flight/UNKNOWN obligation needs "
                         "preservation: the import branch is inapplicable; the legacy records stay as historical evidence"),
            "ownerConfirmation": "PENDING: the business owner confirms that the development-fixture records need not survive"}


def cmd_legacy(a: argparse.Namespace) -> int:
    import tempfile
    import time
    for v in LEGACY_VOLUMES.values():
        if run(["docker", "volume", "inspect", v], check=False).returncode != 0:
            print(f"UNEXECUTED: legacy volume {v} is missing")
            return 2
    name = f"anvilkit-handover-legacy-{a.run.lower()}"
    copy = f"{name}-pg"
    doc = {"schemaVersion": 1, "step": "legacy", "run": a.run, "recordedAt": now(), "image": LEGACY_PG_IMAGE,
           "method": ("the legacy PostgreSQL volume copied into a disposable volume (source mounted read-only), PostgreSQL 18.6 "
                      "started on the copy with no network, socket-only trust authentication and read-only default transactions; "
                      "the intake and artifact volumes read through read-only mounts; nothing of the legacy environment is "
                      "started, written or connected; the copy is removed afterwards. Counts, digests and enumerations only.")}
    hba = pathlib.Path(tempfile.mkdtemp(prefix="anvilkit-handover-hba-"))
    try:
        os.chmod(hba, 0o755)
        (hba / "pg_hba.conf").write_text("local all all trust\n")
        os.chmod(hba / "pg_hba.conf", 0o644)
        run(["docker", "volume", "create", "--label", "anvilkit.io/handover-rehearsal=" + a.run, copy])
        run(["docker", "run", "--rm", "--network", "none", "-v", f"{LEGACY_VOLUMES['postgres']}:/from:ro", "-v", f"{copy}:/to",
             BUSYBOX, "cp", "-a", "/from/.", "/to/"])
        doc["copy"] = {"from": LEGACY_VOLUMES["postgres"], "volume": copy}
        run(["docker", "run", "-d", "--name", name, "--network", "none", "--label", "anvilkit.io/handover-rehearsal=" + a.run,
             "-v", f"{copy}:/var/lib/postgresql", "-v", f"{hba}:/hba:ro", LEGACY_PG_IMAGE,
             "postgres", "-c", "listen_addresses=", "-c", "hba_file=/hba/pg_hba.conf", "-c", "default_transaction_read_only=on"])
        for _ in range(120):
            if run(["docker", "exec", name, "pg_isready", "-U", "postgres"], check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            logs = run(["docker", "logs", "--tail", "40", name], check=False)
            raise RuntimeError("the copy did not become ready: " + (logs.stdout + logs.stderr)[-600:])
        startup = run(["docker", "logs", name], check=False)
        doc["copyStartup"] = [l for l in (startup.stdout + startup.stderr).splitlines()
                              if any(k in l for k in ("recovery", "redo", "database system", "checkpoint"))][:20]
        doc["serverVersion"] = psql(name, "postgres", "SHOW server_version;")[0][0]
        databases = [r[0] for r in psql(name, "postgres", "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 1;")]
        doc["databases"] = {}
        for db in databases:
            tables = psql(name, db, "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                                    "AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2;")
            entry = {"tables": {}, "rows": 0}
            for schema, table in tables:
                q = f"{ident(schema)}.{ident(table)}"
                count, digest = psql(name, db, f"SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) FROM {q} t;")[0]
                cols = [r[0] for r in psql(name, db, f"SELECT column_name FROM information_schema.columns WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY ordinal_position;")]
                t = {"rows": int(count), "md5": digest}
                if int(count):
                    for c in cols:
                        if c in ENUM_COLUMNS or c in IDENTITY_COLUMNS:
                            dist = psql(name, db, f"SELECT coalesce({ident(c)}::text, '<null>'), count(*) FROM {q} GROUP BY 1 ORDER BY 1 LIMIT 50;")
                            t.setdefault("values", {})[c] = {k: int(v) for k, v in dist}
                        if c in ("created_at", "recorded_at", "accepted_at", "updated_at"):
                            lo, hi = psql(name, db, f"SELECT min({ident(c)})::text, max({ident(c)})::text FROM {q};")[0]
                            t.setdefault("timeRange", {})[c] = [lo, hi]
                entry["tables"][f"{schema}.{table}"] = t
                entry["rows"] += int(count)
            doc["databases"][db] = entry
        # Temporal visibility: every workflow execution and the open ones by type.
        vis = next((db for db in databases if "visibility" in db), None)
        if vis:
            rows = psql(name, vis, "SELECT namespace_id, workflow_type_name, status, count(*), min(start_time)::text, max(start_time)::text "
                                   "FROM executions_visibility GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;")
            doc["temporal"] = {"database": vis, "executions": [
                {"namespace": r[0][:8], "workflowType": r[1], "status": TEMPORAL_STATUS.get(int(r[2]), r[2]), "count": int(r[3]),
                 "firstStart": r[4], "lastStart": r[5]} for r in rows]}
            doc["temporal"]["open"] = [
                {"workflowId": r[0], "workflowType": r[1], "startTime": r[2]}
                for r in psql(name, vis, "SELECT workflow_id, workflow_type_name, start_time::text FROM executions_visibility WHERE status = 1 ORDER BY start_time;")]
        doc["volumes"] = {k: volume_files(v) for k, v in LEGACY_VOLUMES.items() if k != "postgres"}
        inputs = evidence_dir(a.run) / "inputs.json"
        doc["assessment"] = assess(doc, json.loads(inputs.read_text()) if inputs.exists() else None)
        doc["result"] = "PASS"
    finally:
        run(["docker", "rm", "-f", name], check=False)
        run(["docker", "volume", "rm", copy], check=False)
        shutil.rmtree(hba, ignore_errors=True)
    out = write(a.run, "legacy-inventory.json", doc)
    for db, e in doc["databases"].items():
        nonempty = {k: v["rows"] for k, v in e["tables"].items() if v["rows"]}
        print(f"{db}: {e['rows']} rows in {len(e['tables'])} tables; non-empty: {nonempty}")
    if doc.get("temporal"):
        print(f"temporal executions: {sum(x['count'] for x in doc['temporal']['executions'])}, open: {len(doc['temporal']['open'])}")
    for k, v in doc["volumes"].items():
        print(f"volume {k}: {len(v['files'])} files")
    print(f"assessment: transfer {doc['assessment']['transferRehearsal']}; rules {json.dumps(doc['assessment']['rules'])}")
    print(f"written {rel(out)}")
    return 0


# ---------------------------------------------------------------- shared access to the installed environment

KUBECTL = str(ROOT / ".local/bin/kubectl")
HANDOVER_NODEPORT = 30911
PROBE_SUBJECT = "sha256:0dc7fa9db7237a2b5c96f70f59bb00f73bb86a0ca5554e91c312f9ada26e18b3"  # local-check-v1 (drills.py)


def k(*args: str, stdin: str | None = None, check: bool = True) -> str:
    p = subprocess.run([KUBECTL, "--kubeconfig", str(QSTATE / "kubeconfig"), *args], input=stdin, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args[:3])}: {p.stderr.strip()[-400:]}")
    return p.stdout


def credentials() -> dict:
    return json.loads((QSTATE / "credentials.json").read_text())


def workers() -> list[str]:
    out = run(["docker", "ps", "--format", "{{.Names}}", "--filter", "name=anvilkit-qualification-worker"]).stdout.split()
    return sorted(out)


def node_ip(node: str) -> str:
    return run(["docker", "inspect", "-f", "{{.NetworkSettings.Networks.kind.IPAddress}}", node]).stdout.strip()


def api_service() -> None:
    """A NodePort Service on the API Pods, owned by the rehearsal (removed by api_service_down)."""
    svc = {"apiVersion": "v1", "kind": "Service",
           "metadata": {"name": "anvilkit-handover-api", "namespace": "anvilkit-apps", "labels": {"anvilkit.io/handover-rehearsal": "true"}},
           "spec": {"type": "NodePort", "selector": {"app.kubernetes.io/name": "anvilkit-agent-api"},
                    "ports": [{"port": 80, "targetPort": "http", "nodePort": HANDOVER_NODEPORT}]}}
    k("apply", "-f", "-", stdin=json.dumps(svc))


def api_service_down() -> None:
    k("-n", "anvilkit-apps", "delete", "service", "anvilkit-handover-api", "--ignore-not-found", check=False)


def call(token: str | None, method: str, path: str, body: bytes | None = None, timeout: float = 10.0) -> tuple[int, str, dict | None]:
    """One request through the rehearsal NodePort: status, raw body (bounded), parsed JSON."""
    import urllib.error
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: tuple[int, str, dict | None] = (0, "no live worker", None)
    for node in workers():
        req = urllib.request.Request(f"http://{node_ip(node)}:{HANDOVER_NODEPORT}{path}", data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except OSError as e:
            last = (0, f"{node}: {e}", None)
            continue
        text = raw.decode("utf-8", "replace")[:2000]
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return status, text, parsed
    return last


def wait_api(seconds: float = 180) -> bool:
    import time
    end = time.time() + seconds
    while time.time() < end:
        if 200 <= call(None, "GET", "/readyz", timeout=3)[0] < 300:
            return True
        time.sleep(2)
    return False


def control_sql(sql: str, db: str = "anvilkit_control") -> list[list[str]]:
    pod = k("-n", "anvilkit-data", "get", "pods", "-l", "cnpg.io/cluster=anvilkit-business,cnpg.io/instanceRole=primary",
            "-o", "jsonpath={.items[0].metadata.name}").strip()
    out = k("-n", "anvilkit-data", "exec", "-i", pod, "-c", "postgres", "--", "psql", "-U", "postgres", "-d", db,
            "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-f", "-", stdin=sql)
    return [line.split("\t") for line in out.splitlines() if line]


def operations_by_tenant() -> dict[str, int]:
    return {t: int(n) for t, n in control_sql("SELECT tenant_id, count(*) FROM operations GROUP BY 1 ORDER BY 1;")}


# ---------------------------------------------------------------- install-check (P24-02)

# Owned schemas, their migration ledgers and the version an empty database reaches.
def expected_migrations() -> dict[str, int]:
    import re
    control = int(re.search(r"const Latest int64 = (\d+)", (ROOT / "services/agent/control/internal/migrate/migrate.go").read_text())[1])
    sql = ROOT / "jobs/migration/internal/migrate/sql"
    return {"anvilkit_control": control,
            "anvilkit_knowledge": len(list((sql / "knowledge").glob("*.sql"))),
            "anvilkit_mcp": len(list((sql / "mcp").glob("*.sql")))}


# Identities of the legacy environment that a clean installation must not name.
# Patterns, not substrings: the new LocalCheck fixture is named anvilkit-local-check-v1.
LEGACY_MARKERS = [r"anvilkit-local(?!-check)", r"anvilkit-local-check(?!-v1)", r":15432\b", r":17233\b", r":18082\b", r":18080\b",
                  r"\banvilkit_agent\b", r"\bagent_control\b", r"\.local/legacy", r"\.local/compose"]


REVIEWED_HOST_PATHS = {"/var/log/pods", "/var/lib/anvilkit-telemetry"}  # the OpenTelemetry Collector DaemonSet (P23-06)


def parse_time(value: str) -> datetime.datetime:
    v = value.strip().replace(" ", "T")
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    t = datetime.datetime.fromisoformat(v)
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def cluster_sql(cluster: str, db: str, sql: str) -> list[list[str]]:
    pod = k("-n", "anvilkit-data", "get", "pods", "-l", f"cnpg.io/cluster={cluster},cnpg.io/instanceRole=primary",
            "-o", "jsonpath={.items[0].metadata.name}").strip()
    out = k("-n", "anvilkit-data", "exec", "-i", pod, "-c", "postgres", "--", "psql", "-U", "postgres", "-d", db,
            "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-f", "-", stdin=sql)
    return [line.split("\t") for line in out.splitlines() if line]


def cmd_install_check(a: argparse.Namespace) -> int:
    ip = evidence_dir(a.run) / "install.json"
    if not ip.exists() or json.loads(ip.read_text()).get("result") != "PASS":
        print(f"UNEXECUTED: {rel(ip)} records no successful installation")
        return 2
    install = json.loads(ip.read_text())
    t0 = parse_time(install["installStartedAt"])
    manifest, mpath = release_manifest(a.release)
    digests = {v["digest"] for v in manifest["images"].values()}
    doc = {"schemaVersion": 1, "step": "install-check", "run": a.run, "release": a.release, "recordedAt": now(),
           "installWindow": [install["installStartedAt"], install["installEndedAt"]], "releaseStatus": manifest["status"]}
    checks = {}
    # 1. Every database cluster bootstrapped from an empty initdb inside the install window.
    clusters = []
    for c in json.loads(k("get", "clusters.postgresql.cnpg.io", "-A", "-o", "json"))["items"]:
        boot = sorted((c["spec"].get("bootstrap") or {}).keys())
        created = c["metadata"]["creationTimestamp"]
        clusters.append({"name": c["metadata"]["name"], "namespace": c["metadata"]["namespace"], "bootstrap": boot, "created": created,
                         "phase": c.get("status", {}).get("phase"), "readyInstances": c.get("status", {}).get("readyInstances"),
                         "image": c["spec"].get("imageName")})
    doc["databaseClusters"] = clusters
    checks["databasesFromEmptyInitdb"] = bool(clusters) and all(x["bootstrap"] == ["initdb"] and parse_time(x["created"]) >= t0 for x in clusters)
    # 2. Every owned schema migrated 00001..N inside the window, nothing earlier.
    migrations = {}
    for db, want in expected_migrations().items():
        rows = cluster_sql("anvilkit-business", db, "SELECT version_id, is_applied, tstamp::text FROM goose_db_version ORDER BY id;")
        versions = [int(r[0]) for r in rows if r[1] == "t"]
        stamps = [parse_time(r[2]) for r in rows]
        migrations[db] = {"expected": want, "applied": versions, "first": min(stamps).isoformat() if stamps else None,
                          "last": max(stamps).isoformat() if stamps else None,
                          "fromZero": versions == list(range(0, want + 1)) and all(t >= t0 for t in stamps)}
    store = cluster_sql("anvilkit-business", "anvilkit_knowledge",
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'memory_store';")
    migrations["anvilkit_knowledge.memory_store"] = {"tables": int(store[0][0])}
    doc["migrations"] = migrations
    checks["schemasMigratedFromZero"] = all(v.get("fromZero", True) for v in migrations.values())
    # 3. A new Temporal: no execution started before the installation.
    tdbs = [r[0] for r in cluster_sql("anvilkit-temporal", "postgres", "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres';")]
    vis = next((d for d in tdbs if "visibility" in d), None)
    temporal = {"databases": tdbs}
    if vis:
        first, count = cluster_sql("anvilkit-temporal", vis, "SELECT coalesce(min(start_time)::text, ''), count(*) FROM executions_visibility;")[0]
        temporal.update({"executions": int(count), "firstStart": first or None})
    main_db = next((d for d in tdbs if d != vis), None)
    if main_db:
        temporal["namespaces"] = [r[0] for r in cluster_sql("anvilkit-temporal", main_db, "SELECT name FROM namespaces ORDER BY 1;")]
    doc["temporal"] = temporal
    checks["temporalHistoriesNew"] = bool(vis) and (not temporal.get("firstStart") or parse_time(temporal["firstStart"]) >= t0)
    # 4. No legacy data directory or volume anywhere: the cluster's volumes are new, the
    #    node containers mount nothing of the legacy project, the legacy environment is unchanged.
    pvs = json.loads(k("get", "pv", "-o", "json"))["items"]
    doc["persistentVolumes"] = {"count": len(pvs), "createdBeforeInstall": [p["metadata"]["name"] for p in pvs if parse_time(p["metadata"]["creationTimestamp"]) < t0]}
    node_mounts = []
    for node in run(["docker", "ps", "--format", "{{.Names}}", "--filter", "name=anvilkit-qualification-"]).stdout.split():
        for m in docker_json("inspect", node)[0]["Mounts"]:
            node_mounts.append({"node": node, "source": m.get("Name") or m["Source"], "destination": m["Destination"]})
    doc["nodeMounts"] = node_mounts
    # A hostPath reaches only the node container's own filesystem (checked above for legacy
    # mounts); the reviewed ones are the telemetry Collector's node log input and buffer (P23-06).
    host_paths, unreviewed = [], []
    for pod in json.loads(k("get", "pods", "-A", "-o", "json"))["items"]:
        for v in pod["spec"].get("volumes", []):
            if "hostPath" in v and pod["metadata"]["namespace"].startswith("anvilkit"):
                entry = f"{pod['metadata']['namespace']}/{pod['metadata']['name']}:{v['hostPath']['path']}"
                host_paths.append(entry)
                if v["hostPath"]["path"] not in REVIEWED_HOST_PATHS:
                    unreviewed.append(entry)
    doc["anvilkitHostPathVolumes"] = {"observed": host_paths, "reviewed": sorted(REVIEWED_HOST_PATHS), "unreviewed": unreviewed}
    frozen = json.loads((evidence_dir(a.run) / "inputs.json").read_text())["legacyEnvironment"]
    current = legacy_environment()
    unchanged = ([(c["name"], c["state"], c["finishedAt"]) for c in frozen["containers"]] == [(c["name"], c["state"], c["finishedAt"]) for c in current["containers"]]
                 and [v["name"] for v in frozen["volumes"]] == [v["name"] for v in current["volumes"]])
    doc["legacyEnvironmentUnchanged"] = unchanged
    checks["noLegacyDataOrVolume"] = (not doc["persistentVolumes"]["createdBeforeInstall"] and not unreviewed and unchanged
                                      and not any("anvilkit-local" in m["source"] for m in node_mounts))
    # 5. Every anvilkit workload runs a release image (by digest).
    foreign = []
    seen = 0
    for ns in ("anvilkit-apps", "anvilkit-components"):
        for pod in json.loads(k("-n", ns, "get", "pods", "-o", "json"))["items"]:
            for st in pod.get("status", {}).get("containerStatuses", []) + pod.get("status", {}).get("initContainerStatuses", []):
                seen += 1
                digest = st.get("imageID", "").rsplit("@", 1)[-1]
                if digest not in digests:
                    foreign.append(f"{ns}/{pod['metadata']['name']}/{st['name']}: {st.get('image')} ({digest[:19]})")
    doc["workloadImages"] = {"containers": seen, "notInRelease": foreign}
    checks["workloadsRunReleaseImages"] = seen > 0 and not foreign
    # 6. Converged and probed.
    apps = json.loads(k("-n", "anvilkit-platform", "get", "applications.argoproj.io", "-o", "json"))["items"]
    unhealthy = [x["metadata"]["name"] for x in apps if (x.get("status", {}).get("sync", {}).get("status"), x.get("status", {}).get("health", {}).get("status")) != ("Synced", "Healthy")]
    probe_jobs = [j for j in json.loads(k("get", "jobs", "-A", "-o", "json"))["items"] if j["metadata"]["name"] == "anvilkit-probe"]
    doc["applications"] = {"count": len(apps), "notSyncedHealthy": unhealthy}
    doc["probe"] = [{"namespace": j["metadata"]["namespace"], "succeeded": j.get("status", {}).get("succeeded", 0),
                     "completed": j.get("status", {}).get("completionTime")} for j in probe_jobs]
    checks["convergedAndProbed"] = bool(apps) and not unhealthy and any(p["succeeded"] for p in doc["probe"])
    # 7. Nothing in the environment's configuration names the legacy environment.
    named = []
    values = QUAL / a.release / "gitops/environment-values.yaml"
    sources = {rel(values): values.read_text()} if values.exists() else {}
    for cm in json.loads(k("get", "configmaps", "-A", "-o", "json"))["items"]:
        if cm["metadata"]["namespace"].startswith("anvilkit"):
            sources[f"configmap {cm['metadata']['namespace']}/{cm['metadata']['name']}"] = json.dumps(cm.get("data") or {})
    import re
    for where, text in sources.items():
        for marker in LEGACY_MARKERS:
            if re.search(marker, text):
                named.append(f"{where}: {marker}")
    doc["configurationNamingLegacy"] = {"sources": len(sources), "matches": named}
    checks["configurationFreeOfLegacy"] = not named
    doc["checks"] = checks
    doc["result"] = "PASS" if all(checks.values()) else "FAIL"
    out = write(a.run, "install-check.json", doc)
    print(f"install-check {doc['result']}: {json.dumps(checks)} ({rel(out)})")
    for x in clusters:
        print(f"  cluster {x['name']}: bootstrap {x['bootstrap']} created {x['created']} {x['phase']}")
    for db, m in migrations.items():
        print(f"  {db}: {m}")
    print(f"  temporal: {temporal}")
    if foreign:
        print("  not in release:", foreign[:10])
    if named:
        print("  legacy names:", named[:10])
    return 0 if doc["result"] == "PASS" else 1


# ---------------------------------------------------------------- dto (P24-04)

NEW_SPEC = ROOT / "contracts/openapi/agent.yaml"
FAKE_ID = "p24-dto-absent"
# Where a legacy request would land if an old client were pointed at the new entry:
# the new endpoint with the same purpose (legacy example name -> method, path).
LEGACY_TO_NEW = {
    "submitLocalCheck.request": ("POST", "/api/v1/operations"),
    "submitGeneration.request": ("POST", "/api/v1/operations"),
    "submitPreview.request": ("POST", "/api/v1/operations"),
    "submitRelease.request": ("POST", "/api/v1/operations"),
    "submitRecertification.request": ("POST", "/api/v1/operations"),
    "submitPreparation.request": ("POST", "/api/v1/preparations"),
    "submitPreparationAnswers.request": ("POST", f"/api/v1/preparations/{FAKE_ID}/answers"),
    "cancelOperation.request": ("POST", f"/api/v1/operations/{FAKE_ID}/commands"),
    "holdOperation.request": ("POST", f"/api/v1/operations/{FAKE_ID}/commands"),
    "resumeOperation.request": ("POST", f"/api/v1/operations/{FAKE_ID}/commands"),
    "resolvePublication.request": ("POST", f"/api/v1/operations/{FAKE_ID}/commands"),
    "applyDefinitionChanges.request": ("POST", f"/api/v1/operations/{FAKE_ID}/commands"),
    "activateDefinition.request": ("POST", f"/api/v1/operations/{FAKE_ID}/commands"),
    "validateDefinition.request": ("POST", f"/api/v1/operations/{FAKE_ID}/commands"),
}
# The legacy example request each legacy route takes (by its operationId in the legacy spec).
LEGACY_EXAMPLE_OF = {"submitLocalCheck", "submitGeneration", "submitPreview", "submitRelease", "submitRecertification", "submitPreparation",
                     "submitPreparationAnswers", "cancelOperation", "holdOperation", "resumeOperation", "resolvePublication",
                     "applyDefinitionChanges", "activateDefinition", "validateDefinition"}


def template_match(template: str, path: str) -> bool:
    import re
    return re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", template), path) is not None


def route_absent(status: int, text: str, parsed: dict | None) -> bool:
    """Gin's own answer for a route the API does not register: 404 and its plain text,
    never the API's error envelope (which a missing resource gets)."""
    return status == 404 and parsed is None and "404 page not found" in text


def cmd_dto(a: argparse.Namespace) -> int:
    for need in (QSTATE / "kubeconfig", QSTATE / "credentials.json"):
        if not need.exists():
            print(f"UNEXECUTED: {rel(need)} is missing (install first)")
            return 2
    legacy_spec = json.loads((ROOT / LEGACY_OPENAPI).read_text())
    examples = json.loads((ROOT / LEGACY_EXAMPLES).read_text())
    new_spec = yaml.safe_load(NEW_SPEC.read_text())
    new_routes = [(m.upper(), "/api/v1" + path) for path, ops in new_spec["paths"].items() for m in ops if m in ("get", "post", "put", "delete", "patch")]
    creds = credentials()
    tenant_a, probe = creds["principals"]["tenantA"], creds["principals"]["probe"]
    inventory = json.loads((evidence_dir(a.run) / "legacy-inventory.json").read_text()) if (evidence_dir(a.run) / "legacy-inventory.json").exists() else {}
    legacy_ops = sorted({f["path"].split("/")[2] for f in inventory.get("volumes", {}).get("artifacts", {}).get("files", [])})
    doc = {"schemaVersion": 1, "step": "dto", "run": a.run, "recordedAt": now(),
           "legacyContract": {"openapi": sha(ROOT / LEGACY_OPENAPI), "examples": sha(ROOT / LEGACY_EXAMPLES)},
           "newContract": {"path": rel(NEW_SPEC), "sha256": sha(NEW_SPEC)}, "cases": []}
    api_service()
    try:
        if not wait_api():
            print("UNEXECUTED: the API does not answer through the rehearsal NodePort")
            return 2
        before = operations_by_tenant()
        doc["operationsBefore"] = before

        def case(group: str, name: str, method: str, path: str, body: bytes | None, token: str | None, expect: str, ok: bool,
                 status: int, text: str, parsed: dict | None) -> None:
            code = ((parsed or {}).get("error") or {}).get("code") if isinstance(parsed, dict) else None
            doc["cases"].append({"group": group, "name": name, "request": f"{method} {path}", "bodySha256": sha_bytes(body) if body else None,
                                 "principal": "tenant_a" if token == tenant_a else ("probe" if token == probe else None),
                                 "expect": expect, "status": status, "errorCode": code, "result": "PASS" if ok else "FAIL",
                                 "answer": None if ok else text[:300]})

        # A/B. Every legacy route, under its own /v1 prefix and under the new /api/v1 prefix.
        for path, ops in legacy_spec["paths"].items():
            if path in ("/healthz", "/readyz"):
                continue
            for m, op in ops.items():
                if m not in ("get", "post", "put", "delete", "patch"):
                    continue
                concrete = path.replace("{operationId}", FAKE_ID).replace("{family}", "component").replace("{definitionId}", FAKE_ID)
                ex = examples.get(f"{op.get('operationId')}.request") if op.get("operationId") in LEGACY_EXAMPLE_OF else None
                body = json.dumps(ex).encode() if ex is not None else None
                for prefix, group in (("/v1", "A legacy route, legacy prefix"), ("/api/v1", "B legacy route, new prefix")):
                    full = prefix + concrete
                    shared = any(nm == m.upper() and template_match(t, full) for nm, t in new_routes)
                    status, text, parsed = call(tenant_a, m.upper(), full, body)
                    if shared:
                        # A read route both contracts share: the new one answers with the new
                        # contract's envelope for an unknown operation (no legacy DTO involved).
                        ok = status == 404 and isinstance(parsed, dict) and parsed.get("error", {}).get("code") == "NOT_FOUND"
                        case(group, f"{op.get('operationId')} (shared read route)", m.upper(), full, body, tenant_a, "404 NOT_FOUND envelope", ok, status, text, parsed)
                    else:
                        case(group, op.get("operationId") or path, m.upper(), full, body, tenant_a, "route absent (404, no envelope)",
                             route_absent(status, text, parsed), status, text, parsed)
        # C. Every legacy request body at the new endpoint of the same purpose.
        for name in sorted(LEGACY_TO_NEW):
            method, path = LEGACY_TO_NEW[name]
            body = json.dumps(examples[name]).encode()
            status, text, parsed = call(tenant_a, method, path, body)
            code = ((parsed or {}).get("error") or {}).get("code") if isinstance(parsed, dict) else None
            case("C legacy DTO, new endpoint", name, method, path, body, tenant_a, "400 INVALID_ARGUMENT", status == 400 and code == "INVALID_ARGUMENT",
                 status, text, parsed)
        # D. The new contract is served: every route answers from the API, never as an unknown route.
        for method, template in new_routes:
            path = template.replace("{operationId}", FAKE_ID).replace("{commandId}", FAKE_ID).replace("{sourceId}", FAKE_ID) \
                .replace("{factId}", FAKE_ID).replace("{serverId}", FAKE_ID).replace("{grantId}", FAKE_ID).replace("{callId}", FAKE_ID) \
                .replace("{handle}", FAKE_ID).replace("{digest}", "sha256:" + "0" * 64)
            body = b"{}" if method in ("POST", "PUT", "PATCH") else None
            status, text, parsed = call(tenant_a, method, path, body)
            case("D new route served", f"{method} {template}", method, path, body, tenant_a, "answered by the API (not an unknown route)",
                 status != 0 and not route_absent(status, text, parsed) and status < 500, status, text, parsed)
        # D'. Positive control: a valid new request is accepted (the probe principal, as the probes chart does).
        body = json.dumps({"commandId": f"p24-dto-{a.run.lower()}", "kind": "local_check",
                           "subject": {"profileId": "local-check-v1", "subjectDigest": PROBE_SUBJECT}}).encode()
        status, text, parsed = call(probe, "POST", "/api/v1/operations", body)
        case("D new route served", "valid CreateOperationRequest (positive control)", "POST", "/api/v1/operations", body, probe, "202 accepted",
             status == 202 and bool((parsed or {}).get("operationId")), status, text, parsed)
        # E. Legacy operation identities are unknown to the new system (no record carried over).
        for op_id in legacy_ops:
            status, text, parsed = call(tenant_a, "GET", f"/api/v1/operations/{op_id}")
            case("E legacy identity", op_id, "GET", f"/api/v1/operations/{op_id}", None, tenant_a, "404 NOT_FOUND envelope",
                 status == 404 and ((parsed or {}).get("error") or {}).get("code") == "NOT_FOUND", status, text, parsed)
        after = operations_by_tenant()
        doc["operationsAfter"] = after
    finally:
        api_service_down()
    changed = {t: (before.get(t, 0), after.get(t, 0)) for t in set(before) | set(after) if before.get(t, 0) != after.get(t, 0)}
    doc["checks"] = {
        "everyCasePassed": all(c["result"] == "PASS" for c in doc["cases"]),
        # Only the positive control may have created an operation, and only for the probe principal.
        "nothingCreatedByLegacyRequests": set(changed) <= {"tenant_probe"} and all(n1 - n0 <= 1 for n0, n1 in changed.values()),
        "tenantAUnchanged": before.get("tenant_a", 0) == after.get("tenant_a", 0),
    }
    doc["result"] = "PASS" if all(doc["checks"].values()) else "FAIL"
    out = write(a.run, "dto.json", doc)
    groups: dict[str, list[int]] = {}
    for c in doc["cases"]:
        g = groups.setdefault(c["group"], [0, 0])
        g[0 if c["result"] == "PASS" else 1] += 1
    for g, (ok, bad) in sorted(groups.items()):
        print(f"{g}: {ok} PASS, {bad} FAIL")
    for c in doc["cases"]:
        if c["result"] != "PASS":
            print(f"  FAIL {c['group']} {c['name']}: {c['request']} -> {c['status']} {c['errorCode']} {(c['answer'] or '')[:160]}")
    print(f"dto {doc['result']}: {json.dumps(doc['checks'])} ({rel(out)})")
    return 0 if doc["result"] == "PASS" else 1


# ---------------------------------------------------------------- withdraw (P24-07)

ROOT_APP = "anvilkit-qualification"
SERVICE_APPS = ["anvilkit-agent-api", "anvilkit-agent-control", "anvilkit-agent-workflow", "anvilkit-agent-model-proxy",
                "anvilkit-agent-knowledge", "anvilkit-agent-mcp", "anvilkit-agent-background-worker"]
AUTOSYNC = {"automated": {"prune": True, "selfHeal": True}, "syncOptions": ["RespectIgnoreDifferences=true"]}
TERMINAL_LIFECYCLES = ("succeeded", "failed", "canceled")


def applications() -> list[dict]:
    return json.loads(k("-n", "anvilkit-platform", "get", "applications.argoproj.io", "-o", "json"))["items"]


def set_root_autosync(enabled: bool) -> None:
    k("-n", "anvilkit-platform", "patch", "applications.argoproj.io", ROOT_APP, "--type", "merge",
      "-p", json.dumps({"spec": {"syncPolicy": AUTOSYNC if enabled else {"automated": None}}}))


def withdraw_apps(names: list[str], timeout: float = 600) -> dict:
    """Delete child Applications; their resources finalizer removes what they deployed."""
    import time
    started = time.time()
    for n in names:
        k("-n", "anvilkit-platform", "delete", "applications.argoproj.io", n, "--wait=false", "--ignore-not-found")
    while time.time() - started < timeout:
        left = [a["metadata"]["name"] for a in applications() if a["metadata"]["name"] in names]
        pods = [p["metadata"]["name"] for p in json.loads(k("-n", "anvilkit-apps", "get", "pods", "-o", "json"))["items"]
                if any(p["metadata"].get("labels", {}).get("app.kubernetes.io/instance", "").startswith(n) for n in names)
                or any(p["metadata"]["name"].startswith(n) for n in names)]
        if not left and not pods:
            return {"seconds": round(time.time() - started, 1), "remainingApplications": [], "remainingPods": []}
        time.sleep(5)
    return {"seconds": round(time.time() - started, 1), "remainingApplications": left, "remainingPods": pods}


def wait_converged(expected: int, timeout: float = 1800) -> dict:
    import time
    started = time.time()
    while time.time() - started < timeout:
        apps = applications()
        bad = [a["metadata"]["name"] for a in apps
               if (a.get("status", {}).get("sync", {}).get("status"), a.get("status", {}).get("health", {}).get("status")) != ("Synced", "Healthy")]
        if len(apps) >= expected and not bad and wait_api(5):
            return {"converged": True, "seconds": round(time.time() - started, 1)}
        time.sleep(10)
    return {"converged": False, "seconds": round(time.time() - started, 1), "notSyncedHealthy": bad}


def entry_closed() -> dict:
    ready = call(None, "GET", "/readyz", timeout=5)
    status, text, _ = call(credentials()["principals"]["tenantA"], "POST", "/api/v1/operations",
                           json.dumps({"commandId": "p24-entry-closed", "kind": "local_check",
                                       "subject": {"profileId": "local-check-v1", "subjectDigest": PROBE_SUBJECT}}).encode(), timeout=5)
    return {"readyz": ready[0], "submit": status, "closed": ready[0] == 0 and status == 0, "detail": text[:160] if status else ready[1][:160]}


def reconciliation(tenant: str, operation_ids: list[str]) -> dict:
    ids = ",".join("'" + i.replace("'", "") + "'" for i in operation_ids) or "''"
    q = lambda sql: control_sql(sql)  # noqa: E731
    ops = q(f"SELECT operation_id, lifecycle, cleanup_state, finance_state, command_id FROM operations WHERE tenant_id = '{tenant}' AND operation_id IN ({ids}) ORDER BY 1;")
    attempts = q(f"SELECT operation_id, count(*), count(*) FILTER (WHERE state <> 'closed' OR cleanup_state IN ('pending', 'unknown')) FROM attempts WHERE operation_id IN ({ids}) GROUP BY 1;")
    launches = q(f"SELECT a.operation_id, count(*), count(*) FILTER (WHERE l.inventory_state <> 'confirmed') FROM launches l JOIN attempts a USING (attempt_id) WHERE a.operation_id IN ({ids}) GROUP BY 1;")
    dispatches = q(f"SELECT count(*) FROM dispatches WHERE operation_id IN ({ids}) AND state IN ('prepared', 'authorized', 'unknown');")
    effects = q(f"SELECT count(*) FROM effect_intents WHERE operation_id IN ({ids}) AND state NOT IN ('succeeded', 'failed', 'confirmed_not_sent', 'denied');")
    commands = q(f"SELECT command_id, count(*) FROM operations WHERE tenant_id = '{tenant}' AND operation_id IN ({ids}) GROUP BY 1 HAVING count(*) > 1;")
    return {
        "operations": [{"id": r[0], "lifecycle": r[1], "cleanup": r[2], "finance": r[3]} for r in ops],
        "found": len(ops),
        "allTerminal": len(ops) == len(operation_ids) and all(r[1] in TERMINAL_LIFECYCLES and r[2] in ("complete", "not_required") and r[3] != "exposure_unknown" for r in ops),
        "openAttempts": sum(int(r[2]) for r in attempts),
        "attemptsPerOperation": {r[0]: int(r[1]) for r in attempts},
        "launchesPerOperation": {r[0]: int(r[1]) for r in launches},
        "unconfirmedLaunches": sum(int(r[2]) for r in launches),
        "unresolvedDispatches": int(dispatches[0][0]),
        "unresolvedEffects": int(effects[0][0]),
        "duplicatedCommands": [r[0] for r in commands],
    }


def cmd_withdraw(a: argparse.Namespace) -> int:
    import time
    for need in (QSTATE / "kubeconfig", QSTATE / "credentials.json"):
        if not need.exists():
            print(f"UNEXECUTED: {rel(need)} is missing (install first)")
            return 2
    tenant_a = credentials()["principals"]["tenantA"]
    doc = {"schemaVersion": 1, "step": "withdraw", "run": a.run, "release": a.release, "recordedAt": now(),
           "mechanism": ("Argo CD: the root Application's automated sync is suspended and the withdrawn child Applications are "
                         "deleted (their resources finalizer removes what they deployed); restoring re-enables the root's automated "
                         "sync, which recreates them from the same signed environment chart. A production withdrawal makes the same "
                         "change through the GitOps source (a reviewed environment revision), not by hand."),
           "phases": {}}
    apps_before = len(applications())
    api_service()
    try:
        if not wait_api():
            print("UNEXECUTED: the API does not answer through the rehearsal NodePort")
            return 2
        # Phase 1: before business writes, the release is withdrawn whole.
        by_tenant = operations_by_tenant()
        p1 = {"operationsByTenant": by_tenant, "noBusinessWrites": set(by_tenant) <= {"tenant_probe"}}
        if not p1["noBusinessWrites"]:
            p1["result"] = "UNEXECUTED"
            p1["reason"] = "business operations exist: the pre-write withdrawal no longer applies to this installation"
        else:
            set_root_autosync(False)
            p1["withdrawn"] = withdraw_apps(SERVICE_APPS)
            p1["entry"] = entry_closed()
            frozen = json.loads((evidence_dir(a.run) / "inputs.json").read_text())["legacyEnvironment"]
            current = legacy_environment()
            p1["legacyUnchanged"] = [(c["name"], c["state"], c["finishedAt"]) for c in frozen["containers"]] == \
                                    [(c["name"], c["state"], c["finishedAt"]) for c in current["containers"]]
            p1["serviceApplicationsLeft"] = [x["metadata"]["name"] for x in applications() if x["metadata"]["name"] in SERVICE_APPS]
            set_root_autosync(True)
            p1["restored"] = wait_converged(apps_before)
            p1["result"] = "PASS" if (not p1["withdrawn"]["remainingApplications"] and not p1["withdrawn"]["remainingPods"]
                                      and p1["entry"]["closed"] and p1["legacyUnchanged"] and not p1["serviceApplicationsLeft"]
                                      and p1["restored"]["converged"]) else "FAIL"
        doc["phases"]["beforeBusinessWrites"] = p1
        print(f"phase 1 (before business writes): {p1['result']}", flush=True)
        # Phase 2: after business writes, admission is closed at the entry and every
        # obligation reconciled before the entry reopens (forward repair).
        accepted = []
        for i in range(a.writes):
            body = json.dumps({"commandId": f"p24-withdraw-{a.run.lower()}-{i}", "kind": "local_check",
                               "subject": {"profileId": "local-check-v1", "subjectDigest": PROBE_SUBJECT}}).encode()
            status, text, parsed = call(tenant_a, "POST", "/api/v1/operations", body)
            if status == 202 and parsed and parsed.get("operationId"):
                accepted.append(parsed["operationId"])
        p2 = {"submitted": a.writes, "accepted": accepted}
        set_root_autosync(False)
        p2["closed"] = withdraw_apps(["anvilkit-agent-api"])
        p2["entry"] = entry_closed()
        p2["stateAtClose"] = reconciliation("tenant_a", accepted)
        deadline = time.time() + a.reconcile_timeout
        while True:
            rec = reconciliation("tenant_a", accepted)
            done = rec["allTerminal"] and not rec["openAttempts"] and not rec["unconfirmedLaunches"] and not rec["unresolvedDispatches"] and not rec["unresolvedEffects"]
            if done or time.time() > deadline:
                break
            time.sleep(10)
        p2["reconciled"] = rec | {"complete": done}
        set_root_autosync(True)
        p2["reopened"] = wait_converged(apps_before)
        reads = {}
        for op in accepted:
            status, _, parsed = call(tenant_a, "GET", f"/api/v1/operations/{op}")
            reads[op] = (parsed or {}).get("lifecycle") if status == 200 else status
        p2["readAfterReopen"] = reads
        p2["result"] = "PASS" if (accepted and p2["entry"]["closed"] and done and not rec["duplicatedCommands"]
                                  and all(n == 1 for n in rec["attemptsPerOperation"].values())
                                  and all(n == 1 for n in rec["launchesPerOperation"].values())
                                  and p2["reopened"]["converged"] and all(v in TERMINAL_LIFECYCLES for v in reads.values())) else "FAIL"
        doc["phases"]["afterBusinessWrites"] = p2
        print(f"phase 2 (after business writes): {p2['result']} ({len(accepted)} accepted)", flush=True)
    finally:
        try:
            set_root_autosync(True)
        finally:
            api_service_down()
    doc["result"] = "PASS" if all(ph.get("result") == "PASS" for ph in doc["phases"].values()) else "FAIL"
    out = write(a.run, "withdraw.json", doc)
    print(f"withdraw {doc['result']} ({rel(out)})")
    return 0 if doc["result"] == "PASS" else 1


# ---------------------------------------------------------------- record (P24-08)

STEP_FILES = {"inputs": "inputs.json", "install": "install.json", "installCheck": "install-check.json", "closure": "closure.json",
              "dto": "dto.json", "legacy": "legacy-inventory.json", "withdraw": "withdraw.json"}


def cmd_record(a: argparse.Namespace) -> int:
    d = evidence_dir(a.run)
    if not (d / "inputs.json").exists():
        print(f"UNEXECUTED: {rel(d / 'inputs.json')} is missing")
        return 2
    manifest, mpath = release_manifest(a.release)
    evidence, results = {}, {}
    for step, name in STEP_FILES.items():
        p = d / name
        if p.exists():
            j = json.loads(p.read_text())
            results[step] = j.get("result") or ("PASS" if step == "inputs" else None)
            evidence[step] = {"path": rel(p), "sha256": sha(p), "result": results[step]}
        else:
            results[step] = "NOT_RUN"
            evidence[step] = {"status": "NOT_RUN"}
    # Earlier attempts are kept beside the record, never dropped.
    attempts = [{"path": rel(p), "sha256": sha(p), "result": json.loads(p.read_text()).get("result"),
                 "failure": json.loads(p.read_text()).get("failure")} for p in sorted(d.glob("install.json.attempt-*"))]
    for e in a.earlier or []:
        for p in sorted((OUT / e).glob("install.json*")) + sorted((OUT / e).glob("bootstrap.log*")):
            attempts.append({"path": rel(p), "sha256": sha(p), "note": f"earlier rehearsal run {e}"})
    legacy = json.loads((d / "legacy-inventory.json").read_text()) if (d / "legacy-inventory.json").exists() else {}
    assessment = legacy.get("assessment") or {}
    inputs = json.loads((d / "inputs.json").read_text())
    exits = {
        "cleanInstallation": results["install"] == "PASS" and results["installCheck"] == "PASS",
        "newBuildClosure": results["closure"] == "PASS",
        "oldDtoSeparation": results["dto"] == "PASS",
        "preservationDetermined": bool(assessment.get("rules")),
        # A REQUIRED transfer would need its own import rehearsal (not built: the evidence makes it inapplicable).
        "transferRehearsalOrInapplicability": assessment.get("transferRehearsal") == "INAPPLICABLE",
        "withdrawalRehearsed": results["withdraw"] == "PASS",
    }
    unclean = sorted(k2 for k2, v in (inputs["release"].get("sourcesClean") or {}).items() if v is False)
    blockers = ["ENV-01..10 absent: DEVELOPMENT_ONLY placement (one host, simulated zones, MinIO for Ceph, CNCF Distribution "
                "for Harbor, a DEVELOPMENT_ONLY signing key)"]
    if manifest["status"] == "BLOCKED":
        blockers.append(f"release {a.release} is {manifest['status']}: {len(manifest.get('findings', []))} blocking findings")
    if unclean:
        blockers.append(f"{len(unclean)} release images built from uncommitted working trees (no reproducible clean-clone build)")
    if assessment.get("ownerConfirmation", "").startswith("PENDING"):
        blockers.append("the business owner has not confirmed that the legacy development-fixture records need not survive")
    blockers += [f"P24a check {k2} not passed" for k2, v in exits.items() if not v]
    p23 = inputs.get("p23Record") or {}
    record = {
        "schemaVersion": 1, "run": a.run, "recordedAt": now(), "placement": "DEVELOPMENT_ONLY",
        "release": {"run": a.release, "manifest": {"path": rel(mpath), "sha256": sha(mpath)}, "status": manifest["status"]},
        "evidence": evidence, "installAttempts": attempts,
        "p24a": {"exit": exits, "transfer": {"rehearsal": assessment.get("transferRehearsal"), "decision": assessment.get("decision"),
                                             "ownerConfirmation": assessment.get("ownerConfirmation"), "rules": assessment.get("rules")},
                 "status": "REHEARSED (DEVELOPMENT_ONLY)" if all(exits.values()) else "INCOMPLETE"},
        "gates": {"G-13": {"status": "NOT_RUN", "rehearsalEvidence": "present" if all(exits.values()) else "partial", "blockedBy": blockers}},
        "p24b": {"status": "BLOCKED", "blockedBy": [
            f"P23 not successful (record {p23.get('path')}: {p23.get('status')}); P24b follows a successful P23",
            "explicit execution authorization for closing the old scope, route/write-authority transfer, destructive "
            "retirement and Git submodule/runtime arrangement changes"]},
    }
    out = d / "handover-rehearsal.json"
    out.write_text(json.dumps(record, indent=2) + "\n")
    lines = [f"# P24a handover rehearsal — run {a.run}", "", f"Status: **{record['p24a']['status']}**; G-13 **NOT_RUN**; P24b **BLOCKED**. "
             f"Release {a.release} ({manifest['status']}). Placement: DEVELOPMENT_ONLY.", "",
             "| Step | Result | Evidence |", "| --- | --- | --- |"]
    for step, e in evidence.items():
        lines.append(f"| {step} | {e.get('result') or e.get('status')} | `{e.get('path', '')}` |")
    lines += ["", "| P24a exit | Met |", "| --- | --- |"] + [f"| {k2} | {'yes' if v else 'no'} |" for k2, v in exits.items()]
    lines += ["", f"Transfer: {assessment.get('transferRehearsal')} — {assessment.get('decision')}.", f"Owner: {assessment.get('ownerConfirmation')}.",
              "", "G-13 blocked by:"] + [f"- {b}" for b in blockers] + ["", "P24b blocked by:"] + [f"- {b}" for b in record["p24b"]["blockedBy"]]
    if attempts:
        lines += ["", "Install attempts kept:"] + [f"- `{x['path']}` {x.get('result') or ''} {x.get('failure') or x.get('note') or ''}" for x in attempts]
    (d / "handover-rehearsal.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nrecord: {rel(out)} {sha(out)}")
    return 0


# ---------------------------------------------------------------- self-test

def cmd_self_test(a: argparse.Namespace) -> int:
    """The rehearsal's decisions on synthetic inputs: the preservation rules (each single
    violation must require a transfer), the legacy-name patterns and the route classifiers."""
    import copy
    import re
    failures = []
    clean = {"databases": {"anvilkit_agent": {"tables": {
        "agent_control.operations": {"rows": 2, "values": {"public_status": {"succeeded": 1, "canceled": 1}, "tenant_id": {"fixture-tenant-a": 2},
                                                           "reserved_amount": {"0": 2}, "confirmed_amount": {"0": 2}, "exposure_amount": {"0": 2},
                                                           "quoted_credits": {"0": 2}}},
        "agent_control.preparations": {"rows": 2, "values": {"resolved": {"true": 2}}},
        **{t: {"rows": 0} for t in LEGACY_EFFECT_TABLES}}}},
        "temporal": {"open": [{"workflowType": "temporal-sys-history-scanner-workflow"}]}}
    inputs = {"studioPages": {"pagesWithRemoteComponentLock": []}}
    if assess(clean, inputs)["transferRequired"]:
        failures.append("a clean inventory requires a transfer")
    mutations = {
        "an operation still running": lambda d: d["databases"]["anvilkit_agent"]["tables"]["agent_control.operations"]["values"]["public_status"].update({"succeeded": 0, "running": 1}),
        "an unresolved preparation": lambda d: d["databases"]["anvilkit_agent"]["tables"]["agent_control.preparations"]["values"]["resolved"].update({"false": 1}),
        "an open business execution": lambda d: d["temporal"]["open"].append({"workflowType": "PreparationWorkflow"}),
        "a cost entry": lambda d: d["databases"]["anvilkit_agent"]["tables"]["agent_control.cost_entries"].update({"rows": 1}),
        "a publication": lambda d: d["databases"]["anvilkit_agent"]["tables"]["agent_control.release_manifests"].update({"rows": 1}),
        "a non-zero amount": lambda d: d["databases"]["anvilkit_agent"]["tables"]["agent_control.operations"]["values"]["confirmed_amount"].update({"12.50": 1}),
        "a real tenant": lambda d: d["databases"]["anvilkit_agent"]["tables"]["agent_control.operations"]["values"]["tenant_id"].update({"acme": 1}),
    }
    for name, mutate in mutations.items():
        d = copy.deepcopy(clean)
        mutate(d)
        if not assess(d, inputs)["transferRequired"]:
            failures.append(f"{name} does not require a transfer")
    if not assess(clean, {"studioPages": {"pagesWithRemoteComponentLock": ["home"]}})["transferRequired"]:
        failures.append("a Studio page lock does not require a transfer")
    if not assess(clean, None)["transferRequired"]:
        failures.append("an unread Studio page store does not require a transfer")
    named = ["name: anvilkit-local", "namespace anvilkit-local-check", "127.0.0.1:15432", "host:17233", "anvilkit_agent", "agent_control.operations",
             ".local/legacy/x", ".local/compose/api.env"]
    new = ["anvilkit-local-check-v1\n", "profile anvilkit-local-check-v1", "anvilkit_agents", "port 154320", "anvilkit_control", "local_check"]
    for t in named:
        if not any(re.search(m, t) for m in LEGACY_MARKERS):
            failures.append(f"legacy name not matched: {t!r}")
    for t in new:
        if any(re.search(m, t) for m in LEGACY_MARKERS):
            failures.append(f"new name matched as legacy: {t!r}")
    if not route_absent(404, "404 page not found", None) or route_absent(404, '{"error":{}}', {"error": {}}) or route_absent(400, "", None):
        failures.append("route_absent misclassifies")
    if not template_match("/api/v1/operations/{operationId}", "/api/v1/operations/x") or template_match("/api/v1/operations/{operationId}", "/api/v1/operations/x/cancel"):
        failures.append("template_match misclassifies")
    for f in failures:
        print("FAIL", f)
    print(f"self-test {'PASS' if not failures else 'FAIL'}: {len(mutations) + 3} preservation cases, {len(named) + len(new)} name probes, route classifiers")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="step", required=True)
    p = sub.add_parser("inputs")
    p.add_argument("--run", required=True)
    p.add_argument("--release", required=True)
    p.set_defaults(func=cmd_inputs)
    p = sub.add_parser("install")
    p.add_argument("--run", required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--wait", type=int, default=3600)
    p.add_argument("--reset-qualification-environment", action="store_true")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("closure")
    p.add_argument("--run", required=True)
    p.add_argument("--release", required=True)
    p.set_defaults(func=cmd_closure)
    p = sub.add_parser("legacy")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_legacy)
    p = sub.add_parser("dto")
    p.add_argument("--run", required=True)
    p.set_defaults(func=cmd_dto)
    p = sub.add_parser("install-check")
    p.add_argument("--run", required=True)
    p.add_argument("--release", required=True)
    p.set_defaults(func=cmd_install_check)
    p = sub.add_parser("withdraw")
    p.add_argument("--run", required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--writes", type=int, default=6)
    p.add_argument("--reconcile-timeout", type=int, default=900)
    p.set_defaults(func=cmd_withdraw)
    p = sub.add_parser("record")
    p.add_argument("--run", required=True)
    p.add_argument("--release", required=True)
    p.add_argument("--earlier", nargs="*", help="earlier rehearsal runs whose install attempts the record keeps")
    p.set_defaults(func=cmd_record)
    p = sub.add_parser("self-test")
    p.set_defaults(func=cmd_self_test)
    a = ap.parse_args()
    if a.step != "self-test" and not shutil.which("docker"):
        print("UNEXECUTED: docker is required")
        return 2
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
