//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
)

// pitrDatabase is Control's database on PostgreSQL 17 with continuous WAL
// archiving and base backups in host directories that stand in for the
// independent DR store (DEVELOPMENT_ONLY: same host). restore replays the
// archive from the base backup to a named restore point into a new
// container, which is what a point-in-time recovery leaves behind: every
// commit after the point is gone from the database while the obligation
// inventory, Temporal and the external world keep it.
type pitrDatabase struct {
	t                 *testing.T
	archive, backup   string
	name              string
	port              string
	containers        []string
	appDSN, migratorD string
}

const pitrImage = "postgres:17-alpine@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"

func dockerCLI(t *testing.T, args ...string) string {
	t.Helper()
	out, err := exec.Command("docker", args...).CombinedOutput()
	require.NoError(t, err, "docker %s: %s", strings.Join(args[:2], " "), out)
	return strings.TrimSpace(string(out))
}

func newPITRDatabase(t *testing.T) *pitrDatabase {
	t.Helper()
	p := startPITRDatabase(t)
	for _, s := range []string{"CREATE ROLE anvilkit_control_app LOGIN PASSWORD 'app'", "CREATE ROLE anvilkit_control_migrator LOGIN PASSWORD 'migrator'",
		"CREATE DATABASE anvilkit_control OWNER anvilkit_control_migrator"} {
		p.psql("postgres", s)
	}
	p.dsns()
	cmd := exec.Command("go", "run", "./cmd/anvilkit-migration")
	cmd.Dir = filepath.Join(repoRoot(t), serviceDirs["anvilkit-agent-control"])
	cmd.Env = append(os.Environ(), "ANVILKIT_MIGRATION_DSN="+p.migratorD, "ANVILKIT_MIGRATION_DEVELOPMENT=true")
	out, err := cmd.CombinedOutput()
	require.NoError(t, err, string(out))
	t.Setenv("ANVILKIT_DEV_CONTROL_DSN", p.appDSN)
	t.Setenv("ANVILKIT_DEV_CONTROL_MIGRATOR_DSN", p.migratorD)
	isolatedTemporalAndObjects(t)
	return p
}

// startPITRDatabase starts an empty archiving server (no domain yet).
func startPITRDatabase(t *testing.T) *pitrDatabase {
	t.Helper()
	base := t.TempDir()
	p := &pitrDatabase{t: t, archive: filepath.Join(base, "archive"), backup: filepath.Join(base, "backup")}
	for _, d := range []string{p.archive, p.backup} {
		require.NoError(t, os.MkdirAll(d, 0o777))
		require.NoError(t, os.Chmod(d, 0o777)) // written by the container's postgres user
	}
	t.Cleanup(func() {
		for _, c := range p.containers {
			_ = exec.Command("docker", "rm", "-f", c).Run()
		}
		// The data directories belong to the container's postgres user.
		_ = exec.Command("docker", "run", "--rm", "-v", base+":/base", pitrImage, "sh", "-c", "rm -rf /base/*").Run()
	})
	p.start(filepath.Join(base, "data-primary"))
	return p
}

// start runs a server on the data directory (initialized on first use, a
// restored base backup later) with archiving on.
func (p *pitrDatabase) start(data string) {
	p.t.Helper()
	require.NoError(p.t, os.MkdirAll(data, 0o777))
	p.name = fmt.Sprintf("anvilkit-pitr-%d-%d", os.Getpid(), len(p.containers))
	p.containers = append(p.containers, p.name)
	dockerCLI(p.t, "run", "-d", "--name", p.name, "-p", "127.0.0.1::5432", "-e", "POSTGRES_PASSWORD=disposable",
		"-v", data+":/var/lib/postgresql/data", "-v", p.archive+":/archive", "-v", p.backup+":/backup", pitrImage,
		"postgres", "-c", "wal_level=replica", "-c", "archive_mode=on", "-c", "archive_command=test ! -f /archive/%f && cp %p /archive/%f")
	// Ready over TCP: on first use the image runs a temporary initdb server on the
	// Unix socket only, and a socket probe could pass just before it shuts down.
	deadline := time.Now().Add(2 * time.Minute)
	for time.Now().Before(deadline) {
		if out, err := exec.Command("docker", "exec", p.name, "psql", "-h", "127.0.0.1", "-U", "postgres", "-Atc", "select pg_is_in_recovery()").CombinedOutput(); err == nil && strings.TrimSpace(string(out)) == "f" {
			mapped := dockerCLI(p.t, "port", p.name, "5432/tcp")
			p.port = mapped[strings.LastIndex(mapped, ":")+1:]
			return
		}
		time.Sleep(time.Second)
	}
	logs, _ := exec.Command("docker", "logs", "--tail", "40", p.name).CombinedOutput()
	p.t.Fatalf("postgres %s did not become a writable primary: %s", p.name, logs)
}

func (p *pitrDatabase) dsns() {
	p.appDSN = fmt.Sprintf("postgres://anvilkit_control_app:app@127.0.0.1:%s/anvilkit_control?sslmode=disable", p.port)
	p.migratorD = fmt.Sprintf("postgres://anvilkit_control_migrator:migrator@127.0.0.1:%s/anvilkit_control?sslmode=disable", p.port)
}

func (p *pitrDatabase) psql(db, sql string) string {
	p.t.Helper()
	return dockerCLI(p.t, "exec", p.name, "psql", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", db, "-Atc", sql)
}

// baseBackup takes a consistent base backup into the DR directory.
func (p *pitrDatabase) baseBackup() {
	p.t.Helper()
	dockerCLI(p.t, "exec", "-u", "postgres", p.name, "pg_basebackup", "-D", "/backup/base", "-X", "stream", "-c", "fast", "--no-password", "-U", "postgres")
}

// mark writes a named restore point: a WAL record after every commit made
// before it and before every commit made after it, which is the recovery
// target. A clock_timestamp() target is not: this host's wall clock runs
// fast and is stepped back by about 1.3 s every half minute (the WSL2 time
// sync), so a time read just after a commit can precede that commit's
// timestamp, and the replay then stops before it (memory scenario run 5).
func (p *pitrDatabase) mark(name string) string {
	p.t.Helper()
	require.Regexp(p.t, `^[a-z0-9-]+$`, name)
	p.psql("postgres", "select pg_create_restore_point('"+name+"')")
	return name
}

// archiveCurrent closes the current WAL segment and waits until it is archived.
func (p *pitrDatabase) archiveCurrent() {
	p.t.Helper()
	segment := p.psql("postgres", "select pg_walfile_name(pg_switch_wal())")
	require.Eventually(p.t, func() bool {
		_, err := os.Stat(filepath.Join(p.archive, segment))
		return err == nil
	}, time.Minute, 500*time.Millisecond, "segment %s archived", segment)
}

// restore loses the running database and recovers the base backup to the
// named restore point into a new container (the old one is removed first:
// one writable primary).
func (p *pitrDatabase) restore(point string) {
	p.t.Helper()
	dockerCLI(p.t, "rm", "-f", p.name)
	data := filepath.Join(filepath.Dir(p.archive), fmt.Sprintf("data-restored-%d", len(p.containers)))
	dockerCLI(p.t, "run", "--rm", "-v", filepath.Dir(p.archive)+":/base", pitrImage, "sh", "-c",
		fmt.Sprintf("cp -a /base/backup/base /base/%s && touch /base/%s/recovery.signal && printf \"restore_command = 'cp /archive/%%%%f %%%%p'\\nrecovery_target_name = '%s'\\nrecovery_target_action = 'promote'\\n\" >> /base/%s/postgresql.auto.conf",
			filepath.Base(data), filepath.Base(data), point, filepath.Base(data)))
	p.start(data)
	p.dsns()
}

// TestPointInTimeRecovery (P23-09) loses Control's database after one
// obligation of each of the five classes and a grant revocation were
// committed, recovers it to a point before them from a base backup and the
// WAL archive, and proves the recovery gates in order: the old writers are
// terminated before the restore; a new recovery epoch closes admission and
// fences every old execution identity before anything is reconciled; the
// inventory window is enumerated completely for all five classes; each lost
// identity comes back from the inventory and is settled only by evidence
// about its original identity, nothing is resent; the revocation the restore
// undid is re-driven by its authority (MCP's REVOKING barrier) before the
// scope reopens, and a revocation committed before the restore point
// survives; only then is admission reopened.
func TestPointInTimeRecovery(t *testing.T) {
	if os.Getenv("KUBECONFIG") == "" {
		skipMissing(t, "KUBECONFIG", "not set; source .local/dev/env.sh after deploy/dev/up.sh")
	}
	db := newPITRDatabase(t)
	root := repoRoot(t)
	dir := t.TempDir()
	e := setupProcesses(t, true, recoveryWorkerConfig(t, root, dir), recoveryControlConfig(t, root, dir), false)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	up := newUpstream(t)
	opsA, execA := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	dispA := controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalModelProxy))
	// Tool sends are admitted only for MCP, their owner (P0.1 allowlist).
	toolA := controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalMCP))
	effA, grantsA := controlv1.NewEffectServiceClient(e.dialControl(e.controlAddr, principalWorkflow)), controlv1.NewGrantPolicyServiceClient(e.dialControl(e.controlAddr, principalMCP))
	recB := controlv1.NewRecoveryServiceClient(e.dialControl(e.controlAddrB, principalAPI))
	findingsB := controlv1.NewRecoveryServiceClient(e.dialControl(e.controlAddrB, principalWorkflow))
	mcp := func(id string) *controlv1.CommandIdentity {
		return &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: id + "-" + run, ActorId: "anvilkit-agent-mcp", RequestDigest: digestOf(id)}
	}
	register := func(grant string) {
		_, err := grantsA.RegisterPolicy(ctx, &controlv1.RegisterPolicyRequest{Command: mcp("register-" + grant), GrantId: grant + "-" + run, GrantRevision: "1",
			PolicyDigest: digestOf(grant), ServerId: "srv", Methods: []string{"read"}})
		require.NoError(t, err)
	}
	revoke := func(grant string) {
		_, err := grantsA.BeginRevocation(ctx, &controlv1.BeginRevocationRequest{Command: mcp("revoke-" + grant), GrantId: grant + "-" + run, GrantRevision: "1"})
		require.NoError(t, err)
	}
	admitTool := func(c controlv1.DispatchServiceClient, grant, opID, attemptID, call string) *controlv1.AdmitToolResponse {
		resp, err := c.AdmitTool(ctx, &controlv1.AdmitToolRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + call + "-" + run, ActorId: "mcp", RequestDigest: digestOf(call)},
			Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
			CallId:  call + "-" + run, Owner: principalMCP, GrantId: grant + "-" + run, GrantRevision: "1", ServerId: "srv", Method: "read", ArgumentDigest: digestOf("args"),
			MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10000"}, Deadline: timestamppb.New(time.Now().Add(2 * time.Minute)),
		})
		require.NoError(t, err)
		return resp
	}

	conn := e.db()
	for _, row := range []struct {
		level         string
		tenant, actor *string
		cap           int64
	}{{"platform", nil, nil, 1_000_000_000}, {"tenant", strPtr("tenant_a"), nil, 100_000_000}, {"actor", strPtr("tenant_a"), strPtr("user_a"), 10_000_000}} {
		_, err := conn.Exec(ctx, "INSERT INTO budget_pools (pool_id, level, tenant_id, actor_id, period_start, period_end, currency, cap_amount) VALUES ($1, $2, $3, $4, now() - interval '1 hour', now() + interval '1 hour', 'USD', $5)",
			"pool-"+run+"-"+row.level, row.level, row.tenant, row.actor, row.cap)
		require.NoError(t, err)
	}
	db.baseBackup()

	// Before the restore point: a revoked grant whose revocation must
	// survive, two grants the later sends use, and a funded operation.
	register("g-revoked-before")
	revoke("g-revoked-before")
	register("g-revoked-after")
	register("g-tool")
	opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "pitr", 100_000)
	target := db.mark("pitr-" + strings.ToLower(run))

	// After the restore point: one obligation of each class and a revocation.
	code, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-lc"))
	require.Equal(t, 202, code, "%v", out)
	lcOp := out["operationId"].(string)
	require.Eventually(t, func() bool {
		var n int
		return conn.QueryRow(ctx, "SELECT count(*) FROM launches WHERE launch_key = $1", launchKey(lcOp)).Scan(&n) == nil && n == 1
	}, 60*time.Second, 250*time.Millisecond, "the LocalCheck run prepared its launch: intake and job-launch obligations exist")
	deadline := timestamppb.New(time.Now().Add(2 * time.Minute))
	model, err := dispA.AdmitModel(ctx, &controlv1.AdmitModelRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-model-" + run, ActorId: "proxy", RequestDigest: digestOf("model")},
		Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
		CallId:  "call-model-" + run, Owner: principalModelProxy, RouteId: "fixture-route", Provider: "fixture", Model: "fixture-model",
		MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10000"}, Deadline: deadline,
	})
	require.NoError(t, err)
	require.True(t, model.GetAdmission().GetDispatchAllowed())
	modelID := model.GetAdmission().GetDispatch().GetDispatchId()
	up.send(t, "call-model-"+run)
	tool := admitTool(toolA, "g-tool", opID, attemptID, "call-tool")
	require.True(t, tool.GetAdmission().GetDispatchAllowed())
	toolID := tool.GetAdmission().GetDispatch().GetDispatchId()
	up.send(t, "call-tool-"+run)
	effect, err := effA.PrepareEffect(ctx, &controlv1.PrepareEffectRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "effect-" + run, ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("effect")},
		Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
		Owner:   "pagix-" + run, Kind: controlv1.EffectKind_EFFECT_KIND_BUSINESS_WRITE, Occurrence: "1", CanonicalSubject: "page:" + run, Deadline: deadline,
	})
	require.NoError(t, err)
	require.True(t, effect.GetPermitted(), "the business write is permitted once: %v", effect)
	effectID := effect.GetEffect().GetEffectId()
	revoke("g-revoked-after")
	for _, class := range []string{"intake", "job-launch", "model-dispatch", "tool-dispatch", "business-write"} {
		entries, err := os.ReadDir(filepath.Join(e.local, "inventory", class))
		require.NoError(t, err)
		require.NotEmpty(t, entries, "the %s obligation is in the independent inventory", class)
	}
	db.archiveCurrent()

	// The database is lost. The old writers are terminated first, then the
	// base backup is recovered to the target from the archive.
	e.stop("control")
	e.stop("control-b")
	db.restore(target)
	restored, err := pgx.Connect(ctx, db.migratorD)
	require.NoError(t, err)
	defer restored.Close(context.Background())
	count := func(sql string, args ...any) int {
		var n int
		require.NoError(t, restored.QueryRow(ctx, sql, args...).Scan(&n))
		return n
	}
	require.Zero(t, count("SELECT count(*) FROM operations WHERE operation_id = $1", lcOp), "the restore lost the intake after the target")
	require.Zero(t, count("SELECT count(*) FROM dispatches WHERE dispatch_id IN ($1, $2)", modelID, toolID), "and both sends")
	require.Zero(t, count("SELECT count(*) FROM effect_intents WHERE effect_id = $1", effectID), "and the business write")
	require.Equal(t, 1, count("SELECT count(*) FROM operations WHERE operation_id = $1", opID), "the operation funded before the target is kept")
	require.Equal(t, 1, count("SELECT count(*) FROM grant_policies WHERE grant_id = $1 AND revocation_state <> 'none'", "g-revoked-before-"+run), "the revocation before the target survives")
	require.Equal(t, 1, count("SELECT count(*) FROM grant_policies WHERE grant_id = $1 AND revocation_state = 'none'", "g-revoked-after-"+run), "the restore undid the later revocation")

	e.controlDSN = db.appDSN
	e.start("control")
	e.waitTCP(e.controlAddr)
	e.start("control-b")
	e.waitTCP(e.controlAddrB)

	// A new recovery epoch before anything else: admission closed, old
	// execution identities fenced.
	operator := &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "recover-" + run, ActorId: "integration-operator", RequestDigest: digestOf("recover-" + run)}
	began, err := recB.BeginRecovery(ctx, &controlv1.BeginRecoveryRequest{
		Scope: operatorScope("tenant_a", "integration-operator"), Command: operator, ScopeKey: "tenant_a", WindowStart: timestamppb.New(time.Now().Add(-time.Hour)), WindowEnd: timestamppb.New(time.Now().Add(time.Minute)),
		ClockUncertainty: durationpb.New(5 * time.Second), Reason: "P23 PITR drill",
	})
	require.NoError(t, err)
	runID := began.GetRun().GetRunId()
	require.Equal(t, controlv1.RecoveryPhase_RECOVERY_PHASE_FENCED, began.GetRun().GetPhase())
	_, err = opsA.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-closed", ActorId: "user_a", RequestDigest: digestOf("closed")},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK, Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "admission is closed while the run is open")
	code, out = e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-closed-api"))
	require.NotEqual(t, 202, code, "the API cannot admit for the closed scope: %v", out)
	// Old exits are fenced: the funded operation's old identity admits nothing,
	// including through the grant whose revocation the restore undid.
	reentry := admitTool(toolA, "g-revoked-after", opID, attemptID, "call-revived")
	require.False(t, reentry.GetAdmission().GetDispatchAllowed(), "the fenced execution identity cannot use the revived grant")
	again, err := dispA.AdmitModel(ctx, &controlv1.AdmitModelRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-model-" + run, ActorId: "proxy", RequestDigest: digestOf("model")},
		Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
		CallId:  "call-model-" + run, Owner: principalModelProxy, RouteId: "fixture-route", Provider: "fixture", Model: "fixture-model",
		MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10000"}, Deadline: deadline,
	})
	require.NoError(t, err)
	require.False(t, again.GetAdmission().GetDispatchAllowed(), "the lost send never regains permission")

	// The grant authority still holds the revocation (MCP's REVOKING barrier)
	// and re-drives it under its original command before the scope reopens.
	revoke("g-revoked-after")
	require.Equal(t, 1, count("SELECT count(*) FROM grant_policies WHERE grant_id = $1 AND revocation_state <> 'none'", "g-revoked-after-"+run), "the revocation is back")

	// The original-identity sources answer for the three send classes; the
	// launch settles on the launcher's evidence; the intake is restored.
	attest := func(class, id string, outcome string, extra map[string]any) {
		body := map[string]any{"class": "outcome", "obligationClass": class, "obligationId": id, "issuer": "integration-supervisor", "sequence": 1,
			"outcome": outcome, "observedAt": time.Now().UTC().Format(time.RFC3339)}
		for k, v := range extra {
			body[k] = v
		}
		raw, _ := json.Marshal(body)
		d := filepath.Join(e.local, "inventory", "outcome", class)
		require.NoError(t, os.MkdirAll(d, 0o750))
		require.NoError(t, os.WriteFile(filepath.Join(d, id), raw, 0o640))
	}
	attest("model-dispatch", modelID, "succeeded", map[string]any{"usage": map[string]uint64{"input_units": 1000, "output_units": 200}})
	attest("tool-dispatch", toolID, "succeeded", map[string]any{"usage": map[string]uint64{"input_units": 10, "output_units": 10}})
	attest("business-write", effectID, "succeeded", map[string]any{"receiptRef": "receipt-" + run})

	var final *controlv1.RecoveryRun
	for until := time.Now().Add(6 * time.Minute); ; time.Sleep(time.Second) {
		got, err := recB.GetRecovery(ctx, &controlv1.GetRecoveryRequest{RunId: runID, Scope: operatorScope("tenant_a", "integration-operator")})
		require.NoError(t, err)
		final = got.GetRun()
		if final.GetPhase() == controlv1.RecoveryPhase_RECOVERY_PHASE_REOPENED {
			break
		}
		if time.Now().After(until) {
			open, _ := findingsB.ListFindings(ctx, &controlv1.ListFindingsRequest{RunId: runID, Limit: 100})
			var lines []string
			for _, f := range open.GetFindings() {
				lines = append(lines, fmt.Sprintf("%s %s %s outcome=%q: %s", f.GetClass(), f.GetObligationId(), f.GetStatus(), f.GetOutcome(), f.GetDetail()))
			}
			for _, name := range []string{"control.log", "control-b.log", "workflow.log"} {
				raw, _ := os.ReadFile(filepath.Join(e.local, name))
				for _, line := range strings.Split(string(raw), "\n") {
					if strings.Contains(line, modelID) || strings.Contains(line, lcOp) || strings.Contains(line, "recover") && strings.Contains(line, "rror") {
						t.Logf("%s: %s", name, line)
					}
				}
			}
			t.Logf("workflow log:\n%s\ncontrol log:\n%s", logTail(t, filepath.Join(e.local, "workflow.log"), 60), logTail(t, filepath.Join(e.local, "control.log"), 30))
			t.Fatalf("run %s did not reopen: phase %s, unsettled %s, progress %v\nfindings:\n%s", runID, final.GetPhase(), final.GetUnsettledFindings(), final.GetProgress(), strings.Join(lines, "\n"))
		}
	}
	require.Equal(t, "0", final.GetUnsettledFindings())
	for _, p := range final.GetProgress() {
		require.True(t, p.GetComplete(), "class %s enumerated completely", p.GetClass())
	}
	findings, err := findingsB.ListFindings(ctx, &controlv1.ListFindingsRequest{RunId: runID, Limit: 200})
	require.NoError(t, err)
	require.True(t, findings.GetComplete())
	byClass := map[string][]*controlv1.Finding{}
	for _, f := range findings.GetFindings() {
		byClass[f.GetClass()] = append(byClass[f.GetClass()], f)
	}
	find := func(class, id string) *controlv1.Finding {
		for _, f := range byClass[class] {
			if f.GetObligationId() == id || (class == "job-launch" && f.GetLaunchKey() == launchKey(lcOp)) {
				return f
			}
		}
		t.Fatalf("no %s finding for %s among %v", class, id, byClass[class])
		return nil
	}
	require.Equal(t, controlv1.FindingStatus_FINDING_STATUS_RESOLVED, find("intake", lcOp).GetStatus(), "the intake is restored under its original identity")
	require.Contains(t, []controlv1.FindingStatus{controlv1.FindingStatus_FINDING_STATUS_RESOLVED, controlv1.FindingStatus_FINDING_STATUS_PRESENT}, find("job-launch", "").GetStatus())
	for class, id := range map[string]string{"model-dispatch": modelID, "tool-dispatch": toolID, "business-write": effectID} {
		f := find(class, id)
		require.Equal(t, controlv1.FindingStatus_FINDING_STATUS_RESOLVED, f.GetStatus(), "%s settled on the original identity's evidence: %v", class, f)
	}
	require.Equal(t, int32(2), up.sends.Load(), "nothing was resent: one model send, one tool send")
	var state string
	require.NoError(t, restored.QueryRow(ctx, "SELECT state FROM dispatches WHERE dispatch_id = $1", modelID).Scan(&state))
	require.Equal(t, "observed", state)

	// Reopened: new work is admitted, the revoked grants admit nothing.
	newOp, newAttempt := e.fundedOperation(ctx, t, opsA, execA, run, "after", 100_000)
	for _, grant := range []string{"g-revoked-before", "g-revoked-after"} {
		resp := admitTool(toolA, grant, newOp, newAttempt, "call-"+grant+"-reopened")
		require.False(t, resp.GetAdmission().GetDispatchAllowed(), "%s stays revoked after the recovery", grant)
	}
	require.True(t, e.workflowClosed(runID), "the reconciliation run completed")
	// Which interleaving this run took: an original owner whose attempt the
	// restore erased keeps its close until the recovery restores the attempt.
	raw, _ := os.ReadFile(filepath.Join(e.local, "workflow.log"))
	kept := strings.Count(string(raw), "the close is kept for the recovery")
	t.Logf("PITR target %s; run %s reopened; findings %d across %d classes; owner closes kept for the recovery: %d", target, runID, len(findings.GetFindings()), len(byClass), kept)
}
