#!/usr/bin/env python3
"""Behavioural proof for contracts/sql (F02, F03, R01, R02), run against a real disposable PostgreSQL.

Starts a throwaway container, applies the three migration steps in their declared order and asserts the
behaviour each remediation item claims. Monotonicity (F02) and result deduplication are proved separately,
as are the three budget-pool levels (F03), the refresh-lease predicates (R01) and each service role (R02).

Nothing here touches a business database: the container is created and destroyed by this script and all
data is clearly synthetic. Exit 0 all passed, 1 a failure, 2 the environment cannot run it (reported as
UNEXECUTED, never as a pass).
"""
import os, subprocess, sys, time, uuid
from concurrent.futures import ThreadPoolExecutor

IMAGE = os.environ.get("ANVILKIT_PG_IMAGE", "postgres:16-alpine")
CTR = os.environ.get("ANVILKIT_PG_CONTAINER", "anvilkit-sql-proof")
DB, USER = "akproof", "postgres"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STEPS = ["contracts/sql/roles-v1.sql", "contracts/definitions/activation-v1.sql", "contracts/sql/control-v1.sql"]
results = []


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def psql(sql, role=None, settings=None):
    """Run one script as `role` (SET ROLE, because the contract roles are NOLOGIN group roles)."""
    prefix = ""
    if role:
        prefix += f"SET ROLE {role};\n"
    for k, v in (settings or {}).items():
        prefix += f"SET {k} = '{v}';\n"
    p = sh(["docker", "exec", "-i", CTR, "psql", "-U", USER, "-d", DB, "-qtA", "-v", "ON_ERROR_STOP=1"],
           input=prefix + sql)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"    {detail}" if detail and not cond else ""))


def ok(name, sql, **kw):
    rc, out, err = psql(sql, **kw)
    check(name, rc == 0, err[:200])
    return out


def denied(name, sql, expect, **kw):
    rc, out, err = psql(sql, **kw)
    check(name, rc != 0 and expect in err, f"rc={rc} err={err[:200]}")


def start():
    if sh(["docker", "info"]).returncode != 0:
        print("UNEXECUTED: docker is not available; the SQL proof did not run", file=sys.stderr)
        sys.exit(2)
    sh(["docker", "rm", "-f", CTR])
    p = sh(["docker", "run", "-d", "--name", CTR, "-e", "POSTGRES_PASSWORD=synthetic",
            "-e", f"POSTGRES_DB={DB}", IMAGE])
    if p.returncode != 0:
        print(f"UNEXECUTED: cannot start {IMAGE}: {p.stderr[:200]}", file=sys.stderr)
        sys.exit(2)
    for _ in range(60):
        if sh(["docker", "exec", CTR, "pg_isready", "-U", USER, "-d", DB]).returncode == 0:
            break
        time.sleep(1)
    else:
        print("UNEXECUTED: PostgreSQL never became ready", file=sys.stderr)
        sys.exit(2)
    ver = sh(["docker", "exec", CTR, "psql", "-U", USER, "-d", DB, "-tAc", "select version()"]).stdout.strip()
    print(f"environment: {ver}\nimage: {IMAGE}\n")


def migrate():
    for i, rel in enumerate(STEPS, 1):
        src = os.path.join(ROOT, rel)
        sh(["docker", "cp", src, f"{CTR}:/tmp/step{i}.sql"])
        p = sh(["docker", "exec", CTR, "psql", "-U", USER, "-d", DB, "-q", "-v", "ON_ERROR_STOP=1",
                "-f", f"/tmp/step{i}.sql"])
        check(f"R02 migration step {i} applies ({rel})", p.returncode == 0, p.stderr[:300])
        if p.returncode != 0:
            summarise()


def seed():
    psql("""
INSERT INTO definition_contract.immutable_records VALUES ('sha256:'||repeat('a',64),'definition','\\x7b7d'::bytea);
INSERT INTO definition_contract.activations VALUES ('act-1','component','def-1','cmd-1','sha256:'||repeat('b',64),'sha256:'||repeat('a',64),'rp','vr','synthetic-owner',now());
INSERT INTO agent_control.operations (operation_id,tenant_id,actor_id,kind,intake_source,command_id,request_digest,activation_id,funding_state,public_status,business_stage,control_state,cleanup_state,accepted_at)
VALUES ('op-1','t-1','a-1','generation','api','cmd-1','sha256:'||repeat('c',64),'act-1','quote_missing','pending','intake','running','not_required',now());
INSERT INTO agent_control.operations (operation_id,tenant_id,actor_id,kind,intake_source,command_id,request_digest,activation_id,funding_state,public_status,business_stage,control_state,cleanup_state,accepted_at)
VALUES ('op-2','t-other','a-9','generation','api','cmd-2','sha256:'||repeat('f',64),'act-1','quote_missing','pending','intake','running','not_required',now());
INSERT INTO agent_control.action_visits (operation_id,action_id,consumed_visits) VALUES ('op-1','act.a',1);
INSERT INTO agent_control.action_counters (operation_id,action_id,counter_kind,value) VALUES ('op-1','act.a','runtime_retries',1);
""")


def f02():
    print("\n-- F02 monotonic counters (guard per table) --")
    ok("F02 action_visits increase succeeds",
       "UPDATE agent_control.action_visits SET consumed_visits=2 WHERE operation_id='op-1'")
    ok("F02 action_visits unchanged value succeeds",
       "UPDATE agent_control.action_visits SET consumed_visits=2 WHERE operation_id='op-1'")
    denied("F02 action_visits decrease rejected",
           "UPDATE agent_control.action_visits SET consumed_visits=1 WHERE operation_id='op-1'",
           "consumed_visits never decreases")
    ok("F02 action_visits unrelated column succeeds",
       "UPDATE agent_control.action_visits SET first_consumed_at=now() WHERE operation_id='op-1'")
    ok("F02 action_counters increase succeeds",
       "UPDATE agent_control.action_counters SET value=2 WHERE operation_id='op-1'")
    ok("F02 action_counters unchanged value succeeds",
       "UPDATE agent_control.action_counters SET value=2 WHERE operation_id='op-1'")
    denied("F02 action_counters decrease rejected",
           "UPDATE agent_control.action_counters SET value=1 WHERE operation_id='op-1'",
           "action counters never decrease")
    ok("F02 action_counters unrelated column succeeds",
       "UPDATE agent_control.action_counters SET updated_at=now() WHERE operation_id='op-1'")
    rc, out, err = psql("UPDATE agent_control.action_counters SET last_source_id='s-x' WHERE operation_id='op-1'")
    check("F02 action_counters update never raises the action_visits rule",
          rc == 0 and "consumed_visits" not in err, err[:200])


def dedup():
    print("\n-- F02 companion: result deduplication is a separate requirement --")
    # a fresh row, because the monotonic guard correctly forbids resetting a counter to zero
    psql("INSERT INTO agent_control.action_visits (operation_id,action_id,consumed_visits) VALUES ('op-1','act.dedup',0)")
    stmt = ("UPDATE agent_control.action_visits SET consumed_visits=consumed_visits+1, last_consuming_result_id='res-1' "
            "WHERE operation_id='op-1' AND action_id='act.dedup' AND last_consuming_result_id IS DISTINCT FROM 'res-1'")
    psql(stmt); psql(stmt)
    v = psql("SELECT consumed_visits FROM agent_control.action_visits WHERE action_id='act.dedup'")[1]
    check("dedup the same result id increments consumed_visits once", v == "1", f"consumed_visits={v}")
    psql("UPDATE agent_control.action_visits SET consumed_visits=consumed_visits+1, last_consuming_result_id='res-2' "
         "WHERE operation_id='op-1' AND action_id='act.dedup' AND last_consuming_result_id IS DISTINCT FROM 'res-2'")
    v = psql("SELECT consumed_visits FROM agent_control.action_visits WHERE action_id='act.dedup'")[1]
    check("dedup a distinct result id does increment", v == "2", f"consumed_visits={v}")
    psql("INSERT INTO agent_control.action_counters (operation_id,action_id,counter_kind,value) "
         "VALUES ('op-1','act.dedup','runtime_retries',0)")
    cstmt = ("UPDATE agent_control.action_counters SET value=value+1, last_source_id='src-1' "
             "WHERE operation_id='op-1' AND action_id='act.dedup' AND counter_kind='runtime_retries' "
             "AND last_source_id IS DISTINCT FROM 'src-1'")
    psql(cstmt); psql(cstmt)
    v = psql("SELECT value FROM agent_control.action_counters WHERE action_id='act.dedup'")[1]
    check("dedup the same source id increments the counter once", v == "1", f"value={v}")


def f03():
    print("\n-- F03 budget-pool logical uniqueness --")
    base = ("INSERT INTO agent_control.budget_pools (pool_id,level,tenant_id,actor_id,currency,period_kind,period_start,cap_amount) "
            "VALUES ('{id}','{lvl}',{t},{a},'{cur}','day','{ps}',1000)")
    ok("F03 global pool inserts", base.format(id="g1", lvl="global", t="NULL", a="NULL", cur="USD", ps="2026-09-10"))
    denied("F03 duplicate global pool rejected",
           base.format(id="g2", lvl="global", t="NULL", a="NULL", cur="USD", ps="2026-09-10"),
           "duplicate key value")
    ok("F03 tenant pool inserts", base.format(id="t1", lvl="tenant", t="'t-1'", a="NULL", cur="USD", ps="2026-09-10"))
    denied("F03 duplicate tenant pool rejected",
           base.format(id="t2", lvl="tenant", t="'t-1'", a="NULL", cur="USD", ps="2026-09-10"),
           "duplicate key value")
    ok("F03 actor pool inserts", base.format(id="a1", lvl="actor", t="'t-1'", a="'a-1'", cur="USD", ps="2026-09-10"))
    denied("F03 duplicate actor pool rejected",
           base.format(id="a2", lvl="actor", t="'t-1'", a="'a-1'", cur="USD", ps="2026-09-10"),
           "duplicate key value")
    ok("F03 distinct currency coexists", base.format(id="g3", lvl="global", t="NULL", a="NULL", cur="EUR", ps="2026-09-10"))
    ok("F03 distinct period coexists", base.format(id="g4", lvl="global", t="NULL", a="NULL", cur="USD", ps="2026-09-11"))
    ok("F03 distinct tenant coexists", base.format(id="t3", lvl="tenant", t="'t-2'", a="NULL", cur="USD", ps="2026-09-10"))
    ok("F03 distinct actor coexists", base.format(id="a3", lvl="actor", t="'t-1'", a="'a-2'", cur="USD", ps="2026-09-10"))

    def race(n):
        return psql(f"BEGIN; SELECT pg_sleep(0.2); INSERT INTO agent_control.budget_pools "
                    f"(pool_id,level,currency,period_kind,period_start,cap_amount) "
                    f"VALUES ('race-{n}','global','GBP','day','2026-09-10',1000); COMMIT;")[0]
    with ThreadPoolExecutor(max_workers=4) as ex:
        codes = list(ex.map(race, range(4)))
    n = psql("SELECT count(*) FROM agent_control.budget_pools WHERE currency='GBP'")[1]
    check("F03 four concurrent creations produce one logical pool", n == "1",
          f"rows={n} exit codes={codes}")


def r01():
    print("\n-- R01 authorization-refresh lease integrity --")
    ins = "INSERT INTO agent_control.authorization_evidence (tenant_id) VALUES ('t-1') ON CONFLICT (tenant_id) DO NOTHING"
    ok("R01 first use creates the row", ins)
    ok("R01 first-use insert is idempotent", ins)
    n = psql("SELECT count(*) FROM agent_control.authorization_evidence WHERE tenant_id='t-1'")[1]
    check("R01 exactly one evidence row after repeated first use", n == "1", f"rows={n}")

    def claim(owner, lease, seconds=12):
        return psql(f"UPDATE agent_control.authorization_evidence SET refresh_owner='{owner}', "
                    f"refresh_lease_id='{lease}', refresh_epoch=refresh_epoch+1, "
                    f"refresh_lease_until=now()+interval '{seconds} seconds', updated_at=now() "
                    f"WHERE tenant_id='t-1' AND (refresh_lease_until IS NULL OR refresh_lease_until < now()) "
                    f"RETURNING refresh_lease_id||' '||refresh_epoch")[1]

    l1 = str(uuid.uuid4()); got1 = claim("replica-A", l1)
    check("R01 lease acquisition returns its own id and epoch", got1.startswith(l1) and got1.endswith(" 1"), got1)
    l2 = str(uuid.uuid4()); got2 = claim("replica-B", l2)
    check("R01 a second replica is refused while the lease is valid", got2 == "", f"returned {got2!r}")

    def complete(owner, lease, epoch, fresh="now()+interval '30 seconds'"):
        return psql(f"UPDATE agent_control.authorization_evidence SET observed_at=now(), fresh_until={fresh}, "
                    f"evidence_revision=evidence_revision+1, members_digest='sha256:'||repeat('1',64), "
                    f"member_roles='{{}}'::jsonb, refresh_owner=NULL, refresh_lease_id=NULL, "
                    f"refresh_lease_until=NULL, updated_at=now() WHERE tenant_id='t-1' AND refresh_owner='{owner}' "
                    f"AND refresh_lease_id='{lease}' AND refresh_epoch={epoch} AND refresh_lease_until > now() "
                    f"AND {fresh} > now() RETURNING evidence_revision")[1]

    check("R01 the holder completes its own acquisition", complete("replica-A", l1, 1) == "1", "")
    check("R01 replaying the completed acquisition writes nothing", complete("replica-A", l1, 1) == "", "")

    l3 = str(uuid.uuid4()); claim("replica-A", l3)
    check("R01 a second round by the same replica gets a new lease id and epoch",
          l3 != l1 and psql("SELECT refresh_epoch FROM agent_control.authorization_evidence WHERE tenant_id='t-1'")[1] == "2")
    check("R01 the first acquisition cannot complete the second", complete("replica-A", l1, 1) == "", "")
    check("R01 the current acquisition completes", complete("replica-A", l3, 2) == "2", "")

    # timeout takeover: expire the lease, let another replica take it, then the former owner returns late
    l4 = str(uuid.uuid4()); claim("replica-A", l4, seconds=-1)  # already expired on acquisition
    l5 = str(uuid.uuid4()); got5 = claim("replica-B", l5)
    check("R01 an expired lease is taken over by another replica", got5.startswith(l5), got5)
    check("R01 the timed-out former owner cannot complete", complete("replica-A", l4, 3) == "", "")
    rel = psql(f"UPDATE agent_control.authorization_evidence SET last_failure_at=now(), refresh_owner=NULL, "
               f"refresh_lease_id=NULL, refresh_lease_until=NULL, updated_at=now() "
               f"WHERE tenant_id='t-1' AND refresh_lease_id='{l4}' AND refresh_epoch=3 RETURNING tenant_id")[1]
    check("R01 the former owner cannot clear the newer lease", rel == "", f"returned {rel!r}")
    held = psql("SELECT refresh_lease_id FROM agent_control.authorization_evidence WHERE tenant_id='t-1'")[1]
    check("R01 the newer lease survives the former owner's release", held == l5, f"held={held!r}")
    check("R01 a read whose freshUntil already passed writes nothing",
          complete("replica-B", l5, 5, fresh="now()-interval '1 second'") == "", "")
    denied("R01 the guard rejects an epoch rollback",
           "UPDATE agent_control.authorization_evidence SET refresh_epoch=0 WHERE tenant_id='t-1'",
           "refresh_epoch never decreases")
    denied("R01 the guard rejects an evidence-revision rollback",
           "UPDATE agent_control.authorization_evidence SET evidence_revision=0 WHERE tenant_id='t-1'",
           "evidence_revision never decreases")


def r02():
    print("\n-- R02 service roles, ownership and grants --")
    owners = psql("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname IN ('agent_control','definition_contract')")[1]
    check("R02 every table is owned by the migrator", owners == "anvilkit_control_migrator", f"owners={owners!r}")
    ok("R02 the migrator can alter its own table",
       "ALTER TABLE agent_control.abuse_counters ADD COLUMN probe_col text", role="anvilkit_control_migrator")
    ok("R02 the migrator can drop what it added",
       "ALTER TABLE agent_control.abuse_counters DROP COLUMN probe_col", role="anvilkit_control_migrator")

    ctl = dict(role="anvilkit_control_rw", settings={"anvilkit.service_context": "control"})
    ok("R02 Control reads the rank-1 activation pointer",
       "SELECT count(*) FROM definition_contract.activation_pointers", **ctl)
    ok("R02 Control writes the rank-1 activation pointer",
       "INSERT INTO definition_contract.activation_pointers (family,definition_id,activation_id) "
       "VALUES ('component','def-1','act-1') ON CONFLICT DO NOTHING", **ctl)
    ok("R02 Control inserts an operation across the schema boundary",
       "INSERT INTO agent_control.operations (operation_id,tenant_id,actor_id,kind,intake_source,command_id,"
       "request_digest,activation_id,funding_state,public_status,business_stage,control_state,cleanup_state,"
       "accepted_at) VALUES ('op-rw','t-1','a-1','generation','api','cmd-rw','sha256:'||repeat('d',64),'act-1',"
       "'quote_missing','pending','intake','running','not_required',now())", **ctl)
    denied("R02 Control cannot delete an operation",
           "DELETE FROM agent_control.operations WHERE operation_id='op-rw'", "permission denied", **ctl)
    denied("R02 Control cannot delete an activation pointer",
           "DELETE FROM definition_contract.activation_pointers", "permission denied", **ctl)
    rc, out, err = psql("SELECT count(*) FROM agent_control.operations", role="anvilkit_control_rw")
    check("R02 Control without its service context sees no rows (NOBYPASSRLS)", out == "0", f"rows={out!r}")

    api = dict(role="anvilkit_api_ro", settings={"anvilkit.tenant_id": "t-1"})
    own = psql("SELECT count(*) FROM agent_control.operations", **api)[1]
    check("R02 the API role reads its own tenant", own != "0" and own.isdigit(), f"rows={own!r}")
    foreign = psql("SELECT count(*) FROM agent_control.operations",
                   role="anvilkit_api_ro", settings={"anvilkit.tenant_id": "t-zzz"})[1]
    check("R02 the API role reads no foreign tenant", foreign == "0", f"rows={foreign!r}")
    denied("R02 the API role cannot write an authority table",
           "INSERT INTO agent_control.operations (operation_id,tenant_id,actor_id,kind,intake_source,command_id,"
           "request_digest,activation_id,funding_state,public_status,business_stage,control_state,cleanup_state,"
           "accepted_at) VALUES ('op-api','t-1','a-1','generation','api','cmd-api','sha256:'||repeat('e',64),"
           "'act-1','quote_missing','pending','intake','running','not_required',now())", "permission denied", **api)
    denied("R02 the API role cannot read budget pools", "SELECT count(*) FROM agent_control.budget_pools",
           "permission denied", **api)
    denied("R02 the API role cannot read authorization evidence",
           "SELECT count(*) FROM agent_control.authorization_evidence", "permission denied", **api)
    denied("R02 the API role cannot reach definition_contract",
           "SELECT count(*) FROM definition_contract.activations", "permission denied", **api)

    psql("CREATE TABLE agent_control.probe_future (id text PRIMARY KEY)", role="anvilkit_control_migrator")
    ok("R02 default privileges cover a later migrator-created table",
       "SELECT count(*) FROM agent_control.probe_future", **ctl)
    psql("DROP TABLE agent_control.probe_future", role="anvilkit_control_migrator")


def summarise():
    failed = [n for n, o, _ in results if not o]
    print(f"\n{len(results)} checks, {len(failed)} failures")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
    sh(["docker", "rm", "-f", CTR])
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    start()
    try:
        migrate(); seed(); f02(); dedup(); f03(); r01(); r02()
    finally:
        pass
    summarise()
