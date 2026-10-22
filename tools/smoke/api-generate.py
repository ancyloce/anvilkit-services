#!/usr/bin/env python3
"""Development smoke client of the public API: one component generated end to
end through the API and followed over Server-Sent Events.

  prompt artifact (API-12 transfer) -> POST /preparations -> SSE until the
  brief is frozen (clarification questions answered with the given text)
  -> POST /operations kind generation against the brief -> SSE until the
  operation is terminal -> GET /operations/{id}/source (a tar) unpacked.

Every SSE frame is recorded (sse-<operationId>.jsonl); the views, receipts
and the final summary are written beside them. The client holds the
DEVELOPMENT_ONLY fixture bearer token of the API (ANVILKIT_DEV_API_TOKEN_A
from .local/dev/env.sh) and nothing else: no provider credential, no
Control access. Exit 0 when the generation succeeded and its source was
downloaded, 1 otherwise (the summary names the failure).

  python3 tools/smoke/api-generate.py --api http://127.0.0.1:9100 \\
    --prompt "..." --evidence outputs/smoke-api-generation/<ts> [--answer "..."]
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid


class ApiError(Exception):
    pass


class Client:
    def __init__(self, api: str, token: str, evidence: pathlib.Path):
        self.api = api.rstrip("/")
        self.token = token
        self.evidence = evidence

    def call(self, method: str, path: str, body=None, timeout: int = 60):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.api + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {}), dict(resp.headers)
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw), dict(error.headers)
            except ValueError:
                return error.code, {"raw": raw.decode(errors="replace")[:2000]}, dict(error.headers)

    def expect(self, want: int, method: str, path: str, body=None, timeout: int = 60):
        code, out, headers = self.call(method, path, body, timeout)
        if code != want:
            raise ApiError(f"{method} {path}: HTTP {code}, wanted {want}: {json.dumps(out)[:1500]}")
        return out, headers

    # API-12: begin, the one PUT the capability authorizes, finalize.
    def upload_artifact(self, run: str, name: str, cls: str, media_type: str, body: bytes) -> dict:
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        transfer, _ = self.expect(201, "POST", "/api/v1/artifacts/transfers",
                                  {"commandId": f"xfer-{run}-{name}", "class": cls, "expectedDigest": digest,
                                   "expectedSize": str(len(body)), "mediaType": media_type})
        if transfer.get("uploadMethod") != "PUT" or not transfer.get("uploadCapability"):
            raise ApiError(f"transfer {name}: no PUT capability in {json.dumps(transfer)[:800]}")
        req = urllib.request.Request(transfer["uploadCapability"], data=body, method="PUT")
        for k, v in (transfer.get("uploadHeaders") or {}).items():
            if k.lower() != "content-length":
                req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=120) as resp:
            if resp.status != 200:
                raise ApiError(f"upload {name}: HTTP {resp.status}")
            version = resp.headers.get("x-amz-version-id")
        if not version:
            raise ApiError(f"upload {name}: the store returned no x-amz-version-id")
        final, _ = self.expect(200, "POST", f"/api/v1/artifacts/{transfer['handle']}/finalizations",
                               {"commandId": f"fin-{run}-{name}", "objectVersion": version})
        return {"transferId": final["transferId"], "digest": digest, "handle": transfer["handle"], "size": len(body)}

    def view(self, operation_id: str) -> dict:
        out, _ = self.expect(200, "GET", f"/api/v1/operations/{operation_id}")
        return out

    def follow(self, operation_id: str, stop, deadline: float, last_id: str = "") -> tuple[dict | None, str]:
        """Reads the SSE stream (resuming with Last-Event-ID across reconnects)
        until stop(frame) is true, the stream ends on a terminal lifecycle,
        or the deadline passes. Returns (the last frame data, last event id)."""
        log = self.evidence / f"sse-{operation_id}.jsonl"
        last_data = None
        while time.time() < deadline:
            req = urllib.request.Request(self.api + f"/api/v1/operations/{operation_id}/events")
            req.add_header("Authorization", "Bearer " + self.token)
            req.add_header("Accept", "text/event-stream")
            if last_id:
                req.add_header("Last-Event-ID", last_id)
            try:
                resp = urllib.request.urlopen(req, timeout=90)
            except urllib.error.HTTPError as error:
                raise ApiError(f"events {operation_id}: HTTP {error.code}: {error.read()[:500]!r}")
            if resp.status != 200 or not resp.headers.get("Content-Type", "").startswith("text/event-stream"):
                raise ApiError(f"events {operation_id}: HTTP {resp.status} {resp.headers.get('Content-Type')}")
            cur = {"id": "", "event": "", "data": None, "retry": ""}
            ended_terminal = False
            try:
                for raw in resp:
                    if time.time() >= deadline:
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                    if line == "":
                        if cur["event"] or cur["data"] is not None:
                            frame = dict(cur)
                            with log.open("a") as f:
                                f.write(json.dumps({"receivedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **frame}) + "\n")
                            payload = (frame["data"] or {}).get("payload", {}) if isinstance(frame["data"], dict) else {}
                            print(f"  sse {operation_id} id={frame['id'] or '-'} {frame['event']} lifecycle={payload.get('lifecycle')} phase={payload.get('phase')} failure={payload.get('failureCode') or ''}", flush=True)
                            if frame["id"]:
                                last_id = frame["id"]
                            if isinstance(frame["data"], dict):
                                last_data = frame["data"]
                            if frame["event"] == "reset_required":
                                raise ApiError(f"events {operation_id}: reset_required {frame['data']}")
                            if frame["event"] == "end":
                                break  # the replica drains: reconnect after retry
                            if frame["event"] == "operation.changed":
                                if stop(frame["data"]):
                                    resp.close()
                                    return last_data, last_id
                                if payload.get("lifecycle") in ("succeeded", "failed", "canceled"):
                                    ended_terminal = True
                        cur = {"id": "", "event": "", "data": None, "retry": ""}
                        continue
                    if line.startswith(":"):
                        continue  # heartbeat
                    k, _, v = line.partition(":")
                    v = v[1:] if v.startswith(" ") else v
                    if k == "id":
                        cur["id"] = v
                    elif k == "event":
                        cur["event"] = v
                    elif k == "retry":
                        cur["retry"] = v
                    elif k == "data":
                        try:
                            cur["data"] = json.loads(v)
                        except ValueError:
                            cur["data"] = {"raw": v}
            except TimeoutError:
                pass  # a quiet stream: reconnect with the cursor
            finally:
                resp.close()
            if ended_terminal:
                return last_data, last_id
            time.sleep(1.0)
        raise ApiError(f"events {operation_id}: deadline passed while following")


def lifecycle_of(frame_or_view: dict) -> str:
    if "payload" in frame_or_view:
        return frame_or_view["payload"].get("lifecycle", "")
    return frame_or_view.get("lifecycle", "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=os.environ.get("ANVILKIT_SMOKE_API_URL", "http://127.0.0.1:9100"))
    ap.add_argument("--token", default=os.environ.get("ANVILKIT_DEV_API_TOKEN_A", ""))
    ap.add_argument("--prompt", default="A HelloCard component: a card that displays one editable title prop as its heading, with a simple background colour, a border and padding. No interactions, images, custom fonts, network requests or additional dependencies. The default title is \"AnvilKit API smoke passed\".")
    ap.add_argument("--answer", default="Keep it minimal: one text field named title, default \"AnvilKit API smoke passed\"; no other fields, no interactions, plain CSS.")
    ap.add_argument("--brief-id", default="", help="reuse a frozen brief instead of running a preparation")
    ap.add_argument("--evidence", default=os.environ.get("ANVILKIT_SMOKE_EVIDENCE_DIR") or f"outputs/smoke-api-generation/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
    ap.add_argument("--preparation-timeout", type=int, default=900)
    ap.add_argument("--generation-timeout", type=int, default=3600)
    ap.add_argument("--max-rounds", type=int, default=3)
    a = ap.parse_args()
    if not a.token:
        print("no API token: set ANVILKIT_DEV_API_TOKEN_A (source .local/dev/env.sh) or --token", file=sys.stderr)
        return 2
    evidence = pathlib.Path(a.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    run = uuid.uuid4().hex[:12]
    c = Client(a.api, a.token, evidence)
    summary = {"run": run, "api": a.api, "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "preparation": {}, "generation": {}, "result": "FAIL"}

    def save(name: str, obj) -> None:
        (evidence / name).write_text(json.dumps(obj, indent=2) + "\n")

    def finish(code: int, error: str = "") -> int:
        if error:
            summary["error"] = error
            print("FAIL:", error, flush=True)
        summary["completedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save("summary.json", summary)
        return code

    try:
        brief_id = a.brief_id
        if not brief_id:
            # Preparation: the prompt as an artifact, the analysis through the
            # Workflow's controlled model call, questions answered in rounds.
            prompt = c.upload_artifact(run, "prompt", "prompt", "text/plain", (a.prompt.strip() + "\n").encode())
            view, _ = c.expect(202, "POST", "/api/v1/preparations",
                               {"commandId": f"prep-{run}", "prompt": {"transferId": prompt["transferId"], "digest": prompt["digest"]}})
            prep_id = view["operationId"]
            summary["preparation"] = {"operationId": prep_id, "prompt": prompt, "rounds": 0}
            print(f"preparation {prep_id} accepted (lifecycle {view['lifecycle']})", flush=True)
            deadline = time.time() + a.preparation_timeout
            last_id = ""
            rounds = 0
            while True:
                frame, last_id = c.follow(prep_id, lambda f: lifecycle_of(f) in ("waiting", "succeeded", "failed", "canceled"), deadline, last_id)
                view = c.view(prep_id)
                save(f"preparation-view-{rounds}.json", view)
                lc = view["lifecycle"]
                if lc == "succeeded":
                    break
                if lc in ("failed", "canceled"):
                    return finish(1, f"preparation {prep_id} {lc}: {view.get('failureCode')} (phase {view.get('phase')})")
                if lc == "waiting" and view.get("clarification"):
                    rounds += 1
                    summary["preparation"]["rounds"] = rounds
                    if rounds > a.max_rounds:
                        return finish(1, f"preparation {prep_id} asked more than {a.max_rounds} rounds of questions")
                    cl = view["clarification"]
                    print(f"  clarification round {cl.get('round')}: {[q.get('text') for q in cl.get('questions', [])]}", flush=True)
                    answers = {q["questionId"]: a.answer for q in cl.get("questions", [])}
                    answer = c.upload_artifact(run, f"answer{rounds}", "answer", "application/json", json.dumps(answers).encode())
                    receipt, _ = c.expect(202, "POST", f"/api/v1/preparations/{prep_id}/answers",
                                          {"commandId": f"ans-{run}-{rounds}", "questionSetId": cl["questionSetId"],
                                           "questionSetRevision": cl["questionSetRevision"], "answer": {"transferId": answer["transferId"], "digest": answer["digest"]}})
                    save(f"answer-receipt-{rounds}.json", receipt)
                    continue
                # waiting without a clarification or a transient state: keep following
                time.sleep(1.0)
            brief_id = (view.get("subject") or {}).get("briefId", "")
            if not brief_id:
                return finish(1, f"preparation {prep_id} succeeded without a briefId: {json.dumps(view)[:800]}")
            summary["preparation"].update({"briefId": brief_id, "phase": view.get("phase")})
            print(f"preparation {prep_id} succeeded: brief {brief_id}", flush=True)

        # Generation against the exact brief.
        subject = "sha256:" + hashlib.sha256(f"generation {run} {brief_id}".encode()).hexdigest()
        view, _ = c.expect(202, "POST", "/api/v1/operations",
                           {"commandId": f"gen-{run}", "kind": "generation", "subject": {"profileId": "generation-v1", "subjectDigest": subject, "briefId": brief_id}})
        gen_id = view["operationId"]
        summary["generation"] = {"operationId": gen_id, "briefId": brief_id}
        print(f"generation {gen_id} accepted (lifecycle {view['lifecycle']})", flush=True)
        deadline = time.time() + a.generation_timeout
        # A run whose settlement waits on an unresolved obligation (an uncertain
        # send) stays reconciling until a recovery disposition: the smoke stops
        # following there and reports it instead of waiting for its timeout.
        def gen_stop(f):
            payload = f.get("payload", {})
            return lifecycle_of(f) in ("succeeded", "failed", "canceled") or (lifecycle_of(f) == "reconciling" and bool(payload.get("failureCode")))
        c.follow(gen_id, gen_stop, deadline)
        view = c.view(gen_id)
        save("generation-view.json", view)
        summary["generation"].update({"lifecycle": view["lifecycle"], "phase": view.get("phase"), "failureCode": view.get("failureCode"), "revision": view.get("revision")})
        if view["lifecycle"] != "succeeded":
            return finish(1, f"generation {gen_id} {view['lifecycle']}: {view.get('failureCode')} (phase {view.get('phase')})")
        print(f"generation {gen_id} succeeded (phase {view.get('phase')})", flush=True)

        # The delivered source: a tar streamed by the API, bound by its digest.
        req = urllib.request.Request(c.api + f"/api/v1/operations/{gen_id}/source")
        req.add_header("Authorization", "Bearer " + c.token)
        with urllib.request.urlopen(req, timeout=120) as resp:
            tar_bytes = resp.read()
            # Header names are case-insensitive (the server writes Anvilkit-Source-*): keep them normalized.
            headers = {k.lower(): v for k, v in resp.headers.items() if k.lower().startswith("anvilkit-")}
        digest = "sha256:" + hashlib.sha256(tar_bytes).hexdigest()
        if headers.get("anvilkit-source-digest") and headers["anvilkit-source-digest"] != digest:
            return finish(1, f"source digest mismatch: header {headers['anvilkit-source-digest']} body {digest}")
        (evidence / "source.tar").write_bytes(tar_bytes)
        out = evidence / "generated-source"
        out.mkdir(exist_ok=True)
        with tarfile.open(fileobj=__import__("io").BytesIO(tar_bytes)) as tar:
            members = [m for m in tar.getmembers() if m.isfile() and not m.name.startswith("/") and ".." not in m.name.split("/")]
            tar.extractall(out, members=members, filter="data")
            files = sorted(m.name for m in members)
        summary["source"] = {"digest": digest, "bytes": len(tar_bytes), "headers": headers, "files": files}
        print(f"source: {len(tar_bytes)} bytes, digest {digest}, revision {headers.get('anvilkit-source-revision')}, files {files}", flush=True)
        summary["result"] = "PASS"
        return finish(0)
    except ApiError as error:
        return finish(1, str(error))


if __name__ == "__main__":
    raise SystemExit(main())
