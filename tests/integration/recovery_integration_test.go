//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
)

// recoveryControlConfig is the dispatch configuration of the recovery
// scenarios. Operator actions are no longer a configured fixture (P0.3):
// each request carries the operator's verified scope (operatorScope).
func recoveryControlConfig(t *testing.T, root, dir string) string {
	t.Helper()
	base, err := os.ReadFile(dispatchControlConfig(t, root, dir))
	require.NoError(t, err)
	text := string(base)
	path := filepath.Join(dir, "control.recovery.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

// recoveryWorkerConfig shortens the worker's settle window and
// reconciliation pacing so the recovery rounds of this scenario, which
// observe a real launch key on the kind cluster, complete within the test.
func recoveryWorkerConfig(t *testing.T, root, dir string) string {
	t.Helper()
	base, err := os.ReadFile(filepath.Join(root, serviceDirs["anvilkit-agent-workflow"], "config.yaml"))
	require.NoError(t, err)
	text := string(base)
	for _, r := range [][2]string{
		{"control_activity_timeout: 30s", "control_activity_timeout: 10s"},
		{"timeout: 3m", "timeout: 60s"},
		{"unresolved_settle_window: 60s", "unresolved_settle_window: 10s"},
		{"reconcile_initial_interval: 5s", "reconcile_initial_interval: 3s"},
		{"reconcile_max_interval: 1m", "reconcile_max_interval: 10s"},
		{"reconcile_max_duration: 24h", "reconcile_max_duration: 5m"},
	} {
		require.Contains(t, text, r[0], "the checked-in configuration still carries the value this scenario overrides")
		text = strings.Replace(text, r[0], r[1], 1)
	}
	path := filepath.Join(dir, "workflow.recovery.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

// TestRecoveryReconciliation runs the P07 loop end to end on the
// development foundation: two Control processes on one database and one
// inventory directory, the real Workflow worker driving RecoveryWorkflow,
// a countable upstream standing in for the provider, and a database that
// loses a dispatch after its single physical send (the row is removed as
// the migrator, which is what a restore to an earlier point leaves behind
// while the inventory keeps the obligation). The operation that carries
// the send is also relayed to a LocalCheck run of its own, so the run's
// launch on the kind cluster is fenced mid-flight by the recovery: its
// finding stays unresolved until the launcher's evidence (observation
// recorded, then the key confirmed empty) or the canceled run's own close
// with definite cleanup settles it; presence alone never does.
func TestRecoveryReconciliation(t *testing.T) {
	isolatedControlDatabase(t)
	root := repoRoot(t)
	migrator := os.Getenv("ANVILKIT_DEV_CONTROL_MIGRATOR_DSN")
	if migrator == "" {
		skipMissing(t, "ANVILKIT_DEV_CONTROL_MIGRATOR_DSN", "not set; source .local/dev/env.sh after deploy/dev/up.sh")
	}
	dir := t.TempDir()
	e := setupProcesses(t, true, recoveryWorkerConfig(t, root, dir), recoveryControlConfig(t, root, dir), false)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	up := newUpstream(t)
	opsA, execA := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	dispA := controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalModelProxy))
	opsB := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddrB, principalAPI))
	// The operator drives the run through the public API (API-17,
	// F-P3.7-1) with a token of the development IdP whose verified roles
	// include operator; Control decides the role and the run's disclosure.
	tokenOperator, tokenOperatorB := oidcToken(t, "operator-a"), oidcToken(t, "operator-b")

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
	t.Cleanup(func() {
		_, _ = conn.Exec(context.Background(), "UPDATE budget_pools SET period_end = period_start + interval '1 second' WHERE pool_id LIKE $1", "pool-"+run+"-%")
	})

	// A completed LocalCheck: its intake and job-launch obligations stay
	// present through the rollback.
	code, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-lc"))
	require.Equal(t, 202, code, "%v", out)
	completed := out["operationId"].(string)
	frames := e.stream(ctx, completed, tokenA, "", terminal)
	require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))

	// One admitted send with an unknown outcome, then the database loses it.
	opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "rec", 50_000)
	digest := digestOf("rec-call")
	deadline := timestamppb.New(time.Now().Add(2 * time.Minute))
	admitted, err := dispA.AdmitModel(ctx, &controlv1.AdmitModelRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-rec", ActorId: "proxy", RequestDigest: digest},
		Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
		CallId:  "call-" + run, Owner: principalModelProxy, RouteId: "fixture-route", Provider: "fixture", Model: "fixture-model",
		MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10000"}, Deadline: deadline,
	})
	require.NoError(t, err)
	require.True(t, admitted.GetAdmission().GetDispatchAllowed())
	dispatchID := admitted.GetAdmission().GetDispatch().GetDispatchId()
	up.send(t, "call-"+run)
	_, err = dispA.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: dispatchID, Source: "proxy", Sequence: "1", Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_UNKNOWN, ObservedAt: timestamppb.Now()})
	require.NoError(t, err)
	require.Equal(t, int32(1), up.sends.Load())

	mig, err := pgx.Connect(ctx, migrator)
	require.NoError(t, err)
	defer mig.Close(context.Background())
	_, err = mig.Exec(ctx, "DELETE FROM cost_entries WHERE dispatch_id = $1", dispatchID)
	require.NoError(t, err)
	_, err = mig.Exec(ctx, "DELETE FROM usage_observations WHERE dispatch_id = $1", dispatchID)
	require.NoError(t, err)
	_, err = mig.Exec(ctx, "DELETE FROM dispatches WHERE dispatch_id = $1", dispatchID)
	require.NoError(t, err)
	// A restore to an earlier point loses the send's reservation with it.
	_, err = mig.Exec(ctx, "UPDATE allocations SET reserved = reserved - 10000 WHERE operation_id = $1", opID)
	require.NoError(t, err)
	var gone int
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM dispatches WHERE dispatch_id = $1", dispatchID).Scan(&gone))
	require.Zero(t, gone)

	// The operation's own LocalCheck run (started by the relay) prepares
	// its launch within a few seconds; the recovery begins once that
	// obligation exists, so the fenced run's launch is part of the window
	// and settles on evidence, never on presence.
	require.Eventually(t, func() bool {
		var n int
		return conn.QueryRow(ctx, "SELECT count(*) FROM launches WHERE launch_key = $1", launchKey(opID)).Scan(&n) == nil && n == 1
	}, 45*time.Second, 250*time.Millisecond, "the relayed LocalCheck run prepared its launch")

	begin := fmt.Sprintf(`{"commandId":"recover-%s","windowStart":%q,"windowEnd":%q,"clockUncertaintySeconds":5,"reason":"integration PITR drill"}`,
		run, time.Now().Add(-time.Hour).UTC().Format(time.RFC3339), time.Now().Add(time.Minute).UTC().Format(time.RFC3339))
	code, out = e.call("POST", "/api/v1/recoveries", tokenA, begin)
	require.Equal(t, 403, code, "only a user holding the operator role begins a run: %v", out)
	code, out = e.call("POST", "/api/v1/recoveries", tokenOperator, begin)
	require.Equal(t, 201, code, "%v", out)
	began := out
	runID := began["runId"].(string)
	require.Equal(t, "fenced", began["phase"])
	require.Equal(t, "tenant_a", began["scopeKey"], "the operator's own tenant")
	code, out = e.call("POST", "/api/v1/recoveries", tokenOperator, begin)
	require.Equal(t, 200, code, "the same command returns the existing run: %v", out)
	require.Equal(t, runID, out["runId"])
	code, out = e.call("POST", "/api/v1/recoveries", tokenOperator, strings.Replace(begin, "recover-"+run, "recover-"+run+"-second", 1))
	require.Equal(t, 409, code, "one run holds the scope: %v", out)
	code, _ = e.call("GET", "/api/v1/recoveries/"+runID, tokenOperatorB, "")
	require.Equal(t, 404, code, "another tenant's operator learns nothing of the run")
	code, _ = e.call("GET", "/api/v1/recoveries/"+runID, tokenA, "")
	require.Equal(t, 404, code, "a member of the tenant without the operator role learns nothing of the run")

	// Admission is closed on both processes for the scope while the run is open.
	_, err = opsA.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-closed", ActorId: "user_a", RequestDigest: digestOf("closed")},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK, Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.Equal(t, codes.FailedPrecondition, status.Code(err))
	require.Contains(t, status.Convert(err).Message(), "RECOVERY_RESTRICTED")
	code, out = e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-closed-api"))
	require.NotEqual(t, 202, code, "the API cannot admit a new operation for the closed scope: %v", out)

	// The relay starts RecoveryWorkflow; the worker enumerates every class,
	// restores the lost dispatch and, with no upstream record of it, leaves
	// the run restricted.
	waitPhase := func(want string, within time.Duration) map[string]any {
		deadline := time.Now().Add(within)
		var last map[string]any
		for time.Now().Before(deadline) {
			code, got := e.call("GET", "/api/v1/recoveries/"+runID, tokenOperator, "")
			require.Equal(t, 200, code, "%v", got)
			last = got
			if last["phase"] == want {
				return last
			}
			time.Sleep(500 * time.Millisecond)
		}
		t.Fatalf("run %s did not reach %s; last %v", runID, want, last)
		return nil
	}
	restricted := waitPhase("restricted", 2*time.Minute)
	for _, p := range restricted["progress"].([]any) {
		require.Equal(t, true, p.(map[string]any)["complete"], "class %v enumerated completely", p)
	}
	require.NotEqual(t, "0", restricted["unsettledFindings"])
	code, findings := e.call("GET", "/api/v1/recoveries/"+runID+"/findings?status=unresolved&limit=10", tokenOperator, "")
	require.Equal(t, 200, code, "%v", findings)
	require.Equal(t, true, findings["complete"])
	byObligation := map[string]map[string]any{}
	for _, raw := range findings["findings"].([]any) {
		f := raw.(map[string]any)
		byObligation[f["obligationId"].(string)] = f
		require.True(t, f["class"] == "model-dispatch" || (f["class"] == "job-launch" && f["launchKey"] == launchKey(opID)), "only the lost send and the fenced run's launch are unsettled: %v", f)
	}
	require.Contains(t, byObligation, dispatchID, "the identity the database lost is back from the inventory, unresolved")
	code, _ = e.call("GET", "/api/v1/recoveries/"+runID+"/findings", tokenOperatorB, "")
	require.Equal(t, 404, code, "another tenant's operator cannot list the run's findings")
	// An operator cannot settle the lost send by assertion: a not-sent
	// confirmation without the owner's independently checkable evidence is
	// refused and records nothing.
	dispose := fmt.Sprintf(`{"commandId":"dispose-%s","class":"model-dispatch","obligationId":%q,"decision":"confirm_not_sent","evidenceRef":"operator-assertion","evidenceDigest":%q,"recoveryEpoch":%q}`,
		run, dispatchID, digestOf("operator-assertion"), began["recoveryEpoch"])
	code, out = e.call("POST", "/api/v1/recoveries/"+runID+"/dispositions", tokenA, dispose)
	require.Equal(t, 403, code, "a disposition needs the operator role: %v", out)
	code, out = e.call("POST", "/api/v1/recoveries/"+runID+"/dispositions", tokenOperator, dispose)
	require.Equal(t, 503, code, "%v", out)
	require.Equal(t, "EFFECT_UNCERTAIN", out["error"].(map[string]any)["code"], "evidence that does not verify is refused")
	var state, currency string
	var reserved int64
	require.NoError(t, conn.QueryRow(ctx, "SELECT state, reserved_currency, reserved_amount FROM dispatches WHERE dispatch_id = $1", dispatchID).Scan(&state, &currency, &reserved))
	require.Equal(t, "unknown", state)
	require.Equal(t, int64(10_000), reserved, "the unresolved exposure is retained, not settled as zero")
	require.Equal(t, int32(1), up.sends.Load(), "nothing is resent under a new identity")
	got, err := dispA.AdmitModel(ctx, &controlv1.AdmitModelRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-rec", ActorId: "proxy", RequestDigest: digest},
		Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
		CallId:  "call-" + run, Owner: principalModelProxy, RouteId: "fixture-route", Provider: "fixture", Model: "fixture-model",
		MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10000"}, Deadline: deadline,
	})
	require.NoError(t, err)
	require.False(t, got.GetAdmission().GetDispatchAllowed(), "the original call never regains permission")
	_, err = opsB.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-still-closed", ActorId: "user_a", RequestDigest: digestOf("closed")},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK, Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "an unresolved outcome keeps the scope restricted")

	// The upstream's record of the original identity appears (the
	// controlled double reads it from the shared inventory); the run's next
	// round records the outcome and reopens the scope.
	attestation, _ := json.Marshal(map[string]any{
		"class": "outcome", "obligationClass": "model-dispatch", "obligationId": dispatchID, "issuer": "integration-supervisor", "sequence": 1,
		"outcome": "succeeded", "usage": map[string]uint64{"input_units": 1200, "output_units": 300}, "observedAt": time.Now().UTC().Format(time.RFC3339),
	})
	outcomeDir := filepath.Join(e.local, "inventory", "outcome", "model-dispatch")
	require.NoError(t, os.MkdirAll(outcomeDir, 0o750))
	require.NoError(t, os.WriteFile(filepath.Join(outcomeDir, dispatchID), attestation, 0o640))
	reopened := waitPhase("reopened", 4*time.Minute)
	require.Equal(t, "0", reopened["unsettledFindings"])
	require.NotNil(t, reopened["reopenedAt"])
	// The fenced run's launch on the cluster settled on evidence, never on
	// presence: the launcher observed and stopped it under the original
	// launch key, or the canceled run closed it with definite cleanup.
	code, all := e.call("GET", "/api/v1/recoveries/"+runID+"/findings?limit=100", tokenOperator, "")
	require.Equal(t, 200, code, "%v", all)
	require.Equal(t, true, all["complete"])
	var launchFinding map[string]any
	for _, raw := range all["findings"].([]any) {
		if f := raw.(map[string]any); f["class"] == "job-launch" && f["launchKey"] == launchKey(opID) {
			launchFinding = f
		}
	}
	require.NotNil(t, launchFinding, "the fenced run's launch obligation was enumerated")
	require.Contains(t, []any{"resolved", "present"}, launchFinding["status"])
	if launchFinding["status"] == "resolved" {
		require.Contains(t, []any{"cleanup_confirmed", "job_stopped"}, launchFinding["outcome"], "%v", launchFinding)
	}
	var attemptState, cleanup string
	require.NoError(t, conn.QueryRow(ctx, "SELECT a.state, coalesce(a.cleanup_state, '') FROM attempts a JOIN launches l ON l.attempt_id = a.attempt_id WHERE l.launch_key = $1", launchKey(opID)).Scan(&attemptState, &cleanup))
	require.Equal(t, "closed", attemptState)
	require.Equal(t, "complete", cleanup, "the launch resolved only with definite cleanup evidence")
	require.NoError(t, conn.QueryRow(ctx, "SELECT state FROM dispatches WHERE dispatch_id = $1", dispatchID).Scan(&state))
	require.Equal(t, "observed", state, "the original outcome is recorded under the original identity")
	var charged int64
	require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(sum(amount), 0) FROM cost_entries WHERE dispatch_id = $1 AND kind IN ('actual', 'correction')", dispatchID).Scan(&charged))
	require.Positive(t, charged, "the metered usage is charged")
	require.Equal(t, int32(1), up.sends.Load())
	created, err := opsA.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-reopened", ActorId: "user_a", RequestDigest: digestOf("reopened")},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK, Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.NoError(t, err, "admission is open again")
	require.NotEmpty(t, created.GetOperation().GetOperationId())
	var epoch int64
	require.NoError(t, conn.QueryRow(ctx, "SELECT recovery_epoch FROM operations WHERE operation_id = $1", opID).Scan(&epoch))
	require.Equal(t, began["recoveryEpoch"], fmt.Sprint(epoch), "the fenced operation carries the run's epoch")
	require.True(t, e.workflowClosed(runID), "the reconciliation run completed")
}
