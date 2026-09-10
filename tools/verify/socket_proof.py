#!/usr/bin/env python3
"""Local Linux proof for the sidecar socket permission and identity contract (F01).

Runs the real three identities against real Unix sockets: the sidecar (UID 10002 with the supplementary
groups the deployment grants it, no CAP_CHOWN and no CAP_DAC_OVERRIDE), the supervisor (UID 0 reduced to
exactly CAP_SETUID, CAP_SETGID and CAP_SETPCAP, so it has no DAC override) and the candidate (UID 10001,
empty supplementary groups, empty capability sets, no_new_privs).

THIS IS NOT gVisor QUALIFICATION. It runs on the host kernel and establishes only that the permission and
peer-credential model in contracts/sidecar/*.json is executable by the identities that hold it. CQ-06
remains pending on the qualified Kubernetes/gVisor/CNI/kernel set.

Exit 0 all passed, 1 a failure, 2 the environment cannot run it (reported as UNEXECUTED, never a pass).
"""
import json, os, shutil, socket, struct, subprocess, sys, tempfile

SIDECAR_UID, CANDIDATE_UID, CANDIDATE_GID, SUPERVISOR_GID = 10002, 10001, 10001, 0
SIDECAR_GID = 10002
KEEP_CAPS = ("cap_setuid", "cap_setgid", "cap_setpcap")
PR_SET_NO_NEW_PRIVS = 38
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"    {detail}" if detail and not cond else ""))


def unexecuted(name, why):
    results.append((name, None, why))
    print(f"UNEXECUTED  {name}    {why}")


def drop_list():
    last = int(open("/proc/sys/kernel/cap_last_cap").read().strip())
    names = []
    for i in range(last + 1):
        r = subprocess.run(["capsh", f"--decode={1 << i:x}"], capture_output=True, text=True).stdout
        n = r.strip().split("=")[-1].strip().lower()
        if n and n not in KEEP_CAPS and "unknown" not in n:
            names.append(n)
    return ",".join(names)


def as_supervisor(argv):
    """Run argv as UID 0 holding exactly the three capabilities the job container adds."""
    return subprocess.run(
        ["capsh", f"--caps={','.join(KEEP_CAPS)}+eip", f"--drop={DROP}", "--", "-c", argv],
        capture_output=True, text=True)


def as_candidate(fn):
    """Fork, become the candidate exactly as DD-03 requires, run fn, exit."""
    pid = os.fork()
    if pid == 0:
        import ctypes
        os.setgroups([])
        os.setresgid(CANDIDATE_GID, CANDIDATE_GID, CANDIDATE_GID)
        os.setresuid(CANDIDATE_UID, CANDIDATE_UID, CANDIDATE_UID)
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        code = 0
        try:
            fn()
        except SystemExit as e:
            code = e.code or 0
        except BaseException as e:  # never let a child fall through into the driver's own code
            print(f"candidate child error: {type(e).__name__}: {e}", flush=True)
            code = 3
        sys.stdout.flush()
        os._exit(code)
    return os.waitpid(pid, 0)[1] >> 8


def connect_result(path):
    """Return None on success, else the errno name."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return None
    except OSError as e:
        return os.strerror(e.errno)
    finally:
        s.close()


# --------------------------------------------------------------------------- the sidecar listener
LISTENER = r'''
import json, os, socket, struct, sys, threading
d, plan = sys.argv[1], json.loads(sys.argv[2])
os.setgroups(plan["supplementaryGroups"])
os.setresgid({sg}, {sg}, {sg}); os.setresuid({su}, {su}, {su})
created = {{}}
socks = {{}}
for name, spec in plan["sockets"].items():
    p = os.path.join(d, name)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(p)
        if spec["gid"] is not None:
            os.chown(p, -1, spec["gid"])
        os.chmod(p, int(spec["mode"], 8))
        s.listen(8); socks[name] = (s, spec)
        st = os.stat(p)
        created[name] = {{"uid": st.st_uid, "gid": st.st_gid, "mode": oct(st.st_mode & 0o777)}}
    except OSError as e:
        created[name] = {{"error": str(e)}}
open(os.path.join(d, "created.json"), "w").write(json.dumps(created))
open(os.path.join(d, "ready"), "w").write("1")
accepted = []
def serve(name, s, spec):
    while True:
        try: c, _ = s.accept()
        except OSError: return
        creds = c.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        allowed = uid == spec["peerUidRequired"]
        accepted.append({{"socket": name, "uid": uid, "gid": gid, "allowed": allowed}})
        open(os.path.join(d, "accepted.jsonl"), "a").write(json.dumps(accepted[-1]) + "\n")
        if not allowed:
            c.sendall(b"PERMISSION_DENIED\n"); c.close(); continue
        try:
            first = c.recv(4096)
            c.sendall(b"OK\n")
            if name == "trusted.sock":
                # one request per connection: anything further is refused and the connection closed
                nxt = c.recv(4096)
                if nxt:
                    c.sendall(b"PERMISSION_DENIED second request\n")
                c.close(); continue
            c.close()
        except OSError:
            pass
for name, (s, spec) in socks.items():
    threading.Thread(target=serve, args=(name, s, spec), daemon=True).start()
import time
time.sleep(float(plan["seconds"]))
'''.format(sg=SIDECAR_GID, su=SIDECAR_UID)


def start_sidecar(d, plan, seconds=25):
    plan = dict(plan, seconds=seconds)
    for f in ("ready", "created.json", "accepted.jsonl"):
        p = os.path.join(d, f)
        if os.path.exists(p):
            os.unlink(p)
    proc = subprocess.Popen([sys.executable, "-c", LISTENER, d, json.dumps(plan)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(200):
        if os.path.exists(os.path.join(d, "ready")):
            break
        import time; time.sleep(0.05)
    else:
        out, err = proc.communicate(timeout=5)
        print("listener failed:", err[:500], file=sys.stderr)
        sys.exit(2)
    return proc, json.load(open(os.path.join(d, "created.json")))


def accepted(d):
    p = os.path.join(d, "accepted.jsonl")
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


# --------------------------------------------------------------------------- phases
def phase_counterexample(base):
    print("\n-- F01 phase 1: the audited counterexample, reproduced --")
    d = os.path.join(base, "before"); os.mkdir(d)
    os.chown(d, SIDECAR_UID, SUPERVISOR_GID); os.chmod(d, 0o711)
    plan = {"supplementaryGroups": [SUPERVISOR_GID, CANDIDATE_GID],
            "sockets": {"trusted.sock": {"gid": None, "mode": "0600", "peerUidRequired": 0}}}
    proc, created = start_sidecar(d, plan, seconds=8)
    st = created["trusted.sock"]
    check("F01 counterexample socket is ownerUid=10002 mode=0600 as recorded",
          st.get("uid") == SIDECAR_UID and st.get("mode") == "0o600", str(st))
    r = as_supervisor(f'{sys.executable} -c "import socket,os,sys;s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n'
                      f'try:\n s.connect(\'{d}/trusted.sock\');print(\'OK\')\nexcept OSError as e:\n print(os.strerror(e.errno))"')
    check("F01 counterexample: the capability-reduced supervisor is refused with EACCES",
          "Permission denied" in r.stdout, f"stdout={r.stdout.strip()!r} stderr={r.stderr.strip()[:200]!r}")
    proc.kill()


def phase_fixed(base):
    print("\n-- F01 phase 2: the corrected permission model --")
    d = os.path.join(base, "run"); os.mkdir(d)
    os.chown(d, SIDECAR_UID, SUPERVISOR_GID); os.chmod(d, 0o711)
    plan = {"supplementaryGroups": [SUPERVISOR_GID, CANDIDATE_GID],
            "sockets": {"trusted.sock": {"gid": SUPERVISOR_GID, "mode": "0660", "peerUidRequired": 0},
                        "candidate.sock": {"gid": CANDIDATE_GID, "mode": "0660", "peerUidRequired": CANDIDATE_UID}}}
    proc, created = start_sidecar(d, plan)
    check("F01 the unprivileged sidecar creates trusted.sock as 10002:0 mode 0660",
          created["trusted.sock"] == {"uid": 10002, "gid": 0, "mode": "0o660"}, str(created["trusted.sock"]))
    check("F01 the unprivileged sidecar creates candidate.sock as 10002:10001 mode 0660",
          created["candidate.sock"] == {"uid": 10002, "gid": 10001, "mode": "0o660"}, str(created["candidate.sock"]))
    st = os.stat(d)
    check("F01 the socket directory is 10002:0 mode 0711 (traversable, never writable by a peer)",
          (st.st_uid, st.st_gid, oct(st.st_mode & 0o777)) == (10002, 0, "0o711"),
          f"{st.st_uid}:{st.st_gid} {oct(st.st_mode & 0o777)}")

    def sup(path):
        return as_supervisor(
            f'{sys.executable} -c "import socket,os\ns=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n'
            f'try:\n s.connect(\'{path}\');s.sendall(b\'req\');print(s.recv(64).decode().strip())\n'
            f'except OSError as e:\n print(os.strerror(e.errno))"')

    r = sup(f"{d}/trusted.sock")
    check("F01 the supervisor connects to the trusted socket and is served",
          r.stdout.strip() == "OK", f"stdout={r.stdout.strip()!r}")
    r = sup(f"{d}/candidate.sock")
    check("F01 the supervisor is refused on the candidate socket",
          "Permission denied" in r.stdout, f"stdout={r.stdout.strip()!r}")

    # the candidate reports through its own writable scratch: it has no write access to the socket directory
    scratch = os.path.join(base, "cand-scratch"); os.mkdir(scratch)
    os.chown(scratch, CANDIDATE_UID, CANDIDATE_GID); os.chmod(scratch, 0o700)
    out = os.path.join(scratch, "cand-out")

    def cand_trusted():
        open(out, "w").write(str(connect_result(f"{d}/trusted.sock")))
    as_candidate(cand_trusted)
    check("F01 the candidate is refused on the trusted socket before any byte is read",
          open(out).read() == "Permission denied", open(out).read())

    def cand_ok():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(f"{d}/candidate.sock"); s.sendall(b"req")
            open(out, "w").write(s.recv(64).decode().strip())
        except OSError as e:
            open(out, "w").write(os.strerror(e.errno))
    as_candidate(cand_ok)
    check("F01 the candidate connects to the candidate socket and is served",
          open(out).read() == "OK", open(out).read())

    acc = accepted(d)
    trusted_peers = [a for a in acc if a["socket"] == "trusted.sock"]
    cand_peers = [a for a in acc if a["socket"] == "candidate.sock"]
    check("F01 SO_PEERCRED reports the supervisor's real UID on the trusted socket",
          bool(trusted_peers) and all(a["uid"] == 0 and a["allowed"] for a in trusted_peers), str(trusted_peers))
    check("F01 SO_PEERCRED reports the candidate's real UID on the candidate socket",
          bool(cand_peers) and all(a["uid"] == CANDIDATE_UID and a["allowed"] for a in cand_peers), str(cand_peers))
    check("F01 no candidate-UID peer was ever accepted on the trusted socket",
          all(a["uid"] != CANDIDATE_UID for a in trusted_peers), str(trusted_peers))

    r = as_supervisor(
        f'{sys.executable} -c "import socket\ns=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n'
        f's.connect(\'{d}/trusted.sock\');s.sendall(b\'one\');print(s.recv(64).decode().strip())\n'
        f's.sendall(b\'two\');print(s.recv(64).decode().strip())"')
    check("F01 a second request on an open trusted connection is refused",
          "PERMISSION_DENIED second request" in r.stdout, f"stdout={r.stdout.strip()!r}")

    def cand_replace():
        got = []
        for op, fn in (("unlink", lambda: os.unlink(f"{d}/trusted.sock")),
                       ("rename", lambda: os.rename(f"{d}/trusted.sock", f"{d}/stolen.sock")),
                       ("create", lambda: open(f"{d}/impostor.sock", "w").close()),
                       ("rmdir-parent", lambda: os.rmdir(d))):
            try:
                fn(); got.append(f"{op}=UNEXPECTED-SUCCESS")
            except OSError as e:
                got.append(f"{op}={os.strerror(e.errno)}")
        open(out, "w").write("; ".join(got))
    as_candidate(cand_replace)
    got = open(out).read()
    check("F01 the candidate cannot unlink, rename, add to or remove the socket directory",
          "UNEXPECTED-SUCCESS" not in got, got)

    def cand_inherit():
        # a connected descriptor handed to a candidate must not survive its exec
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(f"{d}/candidate.sock")
        except OSError:
            pass
        fd = s.fileno()
        inheritable = os.get_inheritable(fd)
        # /bin/sh, not sys.executable: the interpreter may live under a path the candidate cannot traverse
        probe = f"if [ -e /proc/self/fd/{fd} ]; then echo INHERITED; else echo CLOSED; fi"
        try:
            r = subprocess.run(["/bin/sh", "-c", probe], capture_output=True, text=True, close_fds=True)
            child = r.stdout.strip() or f"no-output({r.stderr.strip()[:80]})"
        except OSError as e:
            child = f"spawn-failed({os.strerror(e.errno)})"
        open(out, "w").write(f"inheritable={inheritable} child={child}")
    as_candidate(cand_inherit)
    got = open(out).read()
    check("F01 a connected descriptor is close-on-exec and does not reach an exec'd child",
          "inheritable=False" in got and "CLOSED" in got, got)
    proc.kill()


def phase_readonly(base):
    print("\n-- F01 phase 3: directory replacement denied by a read-only mount --")
    src = os.path.join(base, "rosrc"); dst = os.path.join(base, "romnt")
    os.mkdir(src); os.mkdir(dst)
    os.chown(src, SIDECAR_UID, SUPERVISOR_GID); os.chmod(src, 0o711)
    plan = {"supplementaryGroups": [SUPERVISOR_GID, CANDIDATE_GID],
            "sockets": {"candidate.sock": {"gid": CANDIDATE_GID, "mode": "0660", "peerUidRequired": CANDIDATE_UID}}}
    proc, created = start_sidecar(src, plan, seconds=12)
    scratch = os.path.join(base, "ro-scratch"); os.mkdir(scratch)
    os.chown(scratch, CANDIDATE_UID, CANDIDATE_GID); os.chmod(scratch, 0o700)
    if subprocess.run(["mount", "--bind", src, dst], capture_output=True).returncode != 0 or \
       subprocess.run(["mount", "-o", "remount,ro,bind", dst], capture_output=True).returncode != 0:
        unexecuted("F01 a read-only bind mount still permits connect()",
                   "bind mounts are unavailable in this environment")
        unexecuted("F01 a read-only mount refuses socket replacement", "bind mounts are unavailable")
        proc.kill(); return
    out = os.path.join(scratch, "ro-out")

    def cand():
        r = connect_result(f"{dst}/candidate.sock")
        try:
            os.unlink(f"{dst}/candidate.sock"); w = "UNEXPECTED-SUCCESS"
        except OSError as e:
            w = os.strerror(e.errno)
        open(out, "w").write(f"connect={r}; unlink={w}")
    as_candidate(cand)
    got = open(out).read()
    check("F01 a read-only bind mount still permits connect()", "connect=None" in got, got)
    check("F01 a read-only mount refuses socket replacement", "UNEXPECTED-SUCCESS" not in got, got)
    subprocess.run(["umount", dst], capture_output=True)
    proc.kill()


def summarise():
    failed = [n for n, o, _ in results if o is False]
    skipped = [n for n, o, _ in results if o is None]
    print(f"\n{len([r for r in results if r[1] is not None])} checks, {len(failed)} failures, "
          f"{len(skipped)} unexecuted")
    for n in failed:
        print(f"  FAILED: {n}")
    for n in skipped:
        print(f"  UNEXECUTED: {n}")
    print("\nScope: host-kernel proof of the permission and peer-credential model only. "
          "NOT gVisor qualification; CQ-06 stays pending.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("UNEXECUTED: the proof needs root to hold the three synthetic identities", file=sys.stderr)
        sys.exit(2)
    if not shutil.which("capsh"):
        print("UNEXECUTED: capsh (libcap2-bin) is required to reduce the supervisor's capabilities", file=sys.stderr)
        sys.exit(2)
    DROP = drop_list()
    print(f"environment: kernel {os.uname().release}; supervisor reduced to {','.join(KEEP_CAPS)}\n")
    base = tempfile.mkdtemp(prefix="anvilkit-socket-proof-", dir="/tmp")
    os.chmod(base, 0o755)
    try:
        phase_counterexample(base); phase_fixed(base); phase_readonly(base)
    finally:
        subprocess.run(["umount", os.path.join(base, "romnt")], capture_output=True)
        shutil.rmtree(base, ignore_errors=True)
    summarise()
