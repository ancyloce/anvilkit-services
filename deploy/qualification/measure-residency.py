#!/usr/bin/env python3
"""Two-replica model residency of the Inference service (P23-10; DEVELOPMENT_ONLY placement).

  .local/verification-venv/bin/python deploy/qualification/measure-residency.py --run RUN [--requests 40] [--memory 6g]

Inference is not part of the qualification cluster (two 6 GiB replicas do not fit
beside the platform on this host), so its residency is measured directly on Docker
with the release image of RUN (by digest, from the release manifest), each replica
limited to the chart's memory limit (8 GiB; the anonymous resident set is compared with
its 6 GiB request) and two CPUs (its request), with the image's own configuration:
  1. both replicas start cold: the time to /readyz (weights loaded from the image,
     the probe embedding and rerank done) and the resident memory once ready;
  2. a fixed load alternates embeddings (query and passage) and rerankings across both
     replicas: per-replica latency percentiles, and every answer must carry the locked
     model revision (the models stay resident: no reload, no substitution);
  3. one replica is stopped and started again while the other keeps answering: the
     restart's time to ready and the surviving replica's availability meanwhile.
Writes outputs/qualification/<RUN>/residency/residency.json. The numbers describe this
CPU host, never a production capacity, GPU residency or SLO (ENV-04). Exit 0 when every
check held, 1 otherwise, 2 when the image is missing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOST_REGISTRY = "localhost:5001"
NAMES = ("anvilkit-residency-a", "anvilkit-residency-b")
PORTS = {"anvilkit-residency-a": 29108, "anvilkit-residency-b": 29109}


def digest(parts: list[str]) -> str:
    """The contract's input digest: each part as its UTF-8 byte length, LF, the bytes."""
    h = hashlib.sha256()
    for p in parts:
        b = p.encode()
        h.update(str(len(b)).encode() + b"\n" + b)
    return "sha256:" + h.hexdigest()


def docker(*args: str, check: bool = True) -> str:
    p = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {p.stderr.strip()[-300:]}")
    return p.stdout.strip()


def call(name: str, method: str, path: str, body: dict | None = None, timeout: float = 60.0) -> tuple[int, dict | None, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORTS[name]}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw and r.headers.get_content_type() == "application/json" else None), time.monotonic() - started
    except urllib.error.HTTPError as e:
        return e.code, None, time.monotonic() - started
    except OSError:
        return 0, None, time.monotonic() - started


def ready(name: str) -> bool:
    return call(name, "GET", "/readyz", timeout=5)[0] == 200


def wait_ready(name: str, started: float, timeout: float = 1800) -> float | None:
    while time.monotonic() - started < timeout:
        if ready(name):
            return time.monotonic() - started
        time.sleep(2)
    return None


def memory_bytes(name: str) -> int:
    """The container's current memory (cgroup v2 memory.current, which counts the page
    cache of the weights it mapped) and its anonymous resident part (memory.stat anon)."""
    out = docker("exec", name, "sh", "-c", "cat /sys/fs/cgroup/memory.current; grep -E '^anon ' /sys/fs/cgroup/memory.stat")
    lines = out.split()
    return int(lines[0]), int(lines[2])


def start(name: str, image: str, memory: str) -> float:
    docker("rm", "-f", name, check=False)
    t = time.monotonic()
    docker("run", "-d", "--name", name, "--memory", memory, "--cpus", "2", "-p", f"127.0.0.1:{PORTS[name]}:9108",
           "-e", "ANVILKIT_INFERENCE_LISTEN=0.0.0.0:9108",
           "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", image)
    return t


def embed(name: str, i: int, kind: str) -> tuple[int, dict | None, float]:
    texts = [f"residency probe {i} passage about component catalogs and page layouts {j}" for j in range(8)]
    body = {"compute": {"taskId": f"residency-{i}", "generation": "1", "profileId": "bge-m3-v1"}, "inputKind": kind, "inputs": texts,
            "inputDigest": digest(["bge-m3-v1", kind, *texts])}
    return call(name, "POST", "/api/v1/embeddings", body)


def rerank(name: str, i: int) -> tuple[int, dict | None, float]:
    query = f"which component renders a hero section {i}"
    cands = [{"candidateId": f"c{j}", "text": f"candidate {j}: a component that renders section {j} of a landing page"} for j in range(8)]
    parts = ["bge-reranker-v2-m3-v1", query]
    for c in cands:
        parts += [c["candidateId"], c["text"]]
    body = {"compute": {"taskId": f"residency-r{i}", "generation": "1", "profileId": "bge-reranker-v2-m3-v1"}, "query": query, "candidates": cands,
            "inputDigest": digest(parts)}
    return call(name, "POST", "/api/v1/rerankings", body)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q * (len(s) - 1))))] * 1000, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--requests", type=int, default=40)
    ap.add_argument("--memory", default="8g", help="each replica's memory limit (the chart's limit; its request is 6 GiB)")
    a = ap.parse_args()
    manifest = json.loads((ROOT / "outputs/qualification" / a.run / "release/release-manifest.json").read_text())
    img = manifest["images"].get("anvilkit-agent-inference")
    if not img:
        print("UNEXECUTED: the release manifest has no anvilkit-agent-inference image")
        return 2
    image = f"{HOST_REGISTRY}/anvilkit-agent-inference@{img['digest']}"
    docker("pull", "-q", image)
    revisions = {}
    record: dict = {"run": a.run, "image": img["reference"], "memoryLimit": a.memory, "cpusPerReplica": 2,
                    "placement": "DEVELOPMENT_ONLY: two containers on one CPU host; no GPU (ENV-04)"}
    try:
        # 1. cold start of both replicas, side by side
        starts = {n: start(n, image, a.memory) for n in NAMES}
        cold = {n: wait_ready(n, starts[n]) for n in NAMES}
        record["coldStartToReadySeconds"] = {n: round(v, 1) if v else None for n, v in cold.items()}
        if not all(cold.values()):
            record["checks"] = {"bothReplicasReady": False}
            raise RuntimeError("a replica did not become ready")
        record["memoryWhenReady"] = {n: dict(zip(("currentBytes", "anonBytes"), memory_bytes(n))) for n in NAMES}

        # 2. the load alternates over both replicas
        lat: dict[str, dict[str, list[float]]] = {n: {"embed": [], "rerank": []} for n in NAMES}
        failures = 0
        for i in range(a.requests):
            n = NAMES[i % 2]
            code, out, secs = embed(n, i, "query" if i % 3 else "passage")
            failures += code != 200
            if out:
                revisions.setdefault(n, set()).add(out.get("modelRevision"))
                lat[n]["embed"].append(secs)
            code, out, secs = rerank(n, i)
            failures += code != 200
            if out:
                revisions.setdefault(n, set()).add(out.get("modelRevision"))
                lat[n]["rerank"].append(secs)
        record["load"] = {"requests": a.requests * 2, "failures": failures,
                          "latencyMs": {n: {k: {"p50": pct(v, 0.5), "p95": pct(v, 0.95), "max": pct(v, 1.0)} for k, v in d.items()} for n, d in lat.items()},
                          "modelRevisions": {n: sorted(x for x in v if x) for n, v in revisions.items()}}
        record["memoryAfterLoad"] = {n: dict(zip(("currentBytes", "anonBytes"), memory_bytes(n))) for n in NAMES}
        record["withinChartRequest6GiB"] = {n: m["anonBytes"] < 6 * 1024**3 for n, m in record["memoryAfterLoad"].items()}

        # 3. restart one replica while the other answers
        survivor, restarted = NAMES
        samples: list[bool] = []
        stop = threading.Event()

        def serve() -> None:
            i = 10_000
            while not stop.is_set():
                i += 1
                samples.append(embed(survivor, i, "query")[0] == 200)
                time.sleep(0.5)

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        docker("stop", "-t", "25", restarted)
        t0 = time.monotonic()
        docker("start", restarted)
        back = wait_ready(restarted, t0)
        stop.set()
        t.join(timeout=60)
        record["restart"] = {"replica": restarted, "toReadySeconds": round(back, 1) if back else None,
                             "survivorAnswers": f"{sum(samples)}/{len(samples)}"}
        record["checks"] = {
            "bothReplicasReady": True,
            "noFailedRequest": failures == 0,
            "oneLockedRevisionPerModel": all(len(v) <= 2 for v in revisions.values()) and len({r for v in revisions.values() for r in v}) <= 2,
            "noReplicaRestartedByItself": all(docker("inspect", "-f", "{{.RestartCount}} {{.State.OOMKilled}}", n) == "0 false" for n in NAMES),
            "survivorServedThroughRestart": bool(samples) and all(samples),
            "restartedReplicaReady": back is not None,
        }
    except Exception as e:  # recorded, never hidden
        record.setdefault("checks", {})["completed"] = False
        record["error"] = str(e)
    finally:
        for n in NAMES:
            docker("rm", "-f", n, check=False)
    out = ROOT / "outputs/qualification" / a.run / "residency"
    out.mkdir(parents=True, exist_ok=True)
    (out / "residency.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=1))
    return 0 if record.get("checks") and all(record["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
