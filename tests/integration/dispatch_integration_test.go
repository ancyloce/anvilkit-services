//go:build integration

package integration

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
)

// dispatchControlConfig is the Control configuration of the P06 scenarios:
// the checked-in file with the DEVELOPMENT_ONLY dispatch fixtures enabled
// (one priced model route authorized for tenant_a, one priced tool method
// and one trusted not-sent issuer). Every value still passes the service's
// own validation; no real price, provider or authorization is involved.
func dispatchControlConfig(t *testing.T, root, dir string) string {
	t.Helper()
	base, err := os.ReadFile(filepath.Join(root, serviceDirs["anvilkit-agent-control"], "config.yaml"))
	require.NoError(t, err)
	text := string(base)
	const fixtures = `  development:
    enabled: true
    prices:
      - revision: fx-int-model
        kind: model
        route: fixture-route
        provider: fixture
        model: fixture-model
        currency: USD
        effective_from: 2026-01-01T00:00:00Z
        effective_until: 2027-01-01T00:00:00Z
        per_million: {input_units: "3000000", output_units: "15000000", reasoning_units: "15000000", cached_input_units: "300000"}
        max_exposure: "1000000"
      - revision: fx-int-tool
        kind: tool
        route: srv/read
        currency: USD
        effective_from: 2026-01-01T00:00:00Z
        effective_until: 2027-01-01T00:00:00Z
        per_million: {input_units: "3000000", output_units: "15000000", reasoning_units: "15000000", cached_input_units: "300000"}
        max_exposure: "1000000"
    authorized_routes:
      - tenant_id: tenant_a
        route_id: fixture-route
    not_sent_issuers: [integration-supervisor]`
	// The checked-in file selects the DEVELOPMENT_ONLY dispatch chain with
	// no fixture (P2.1); the scenarios add theirs.
	require.Contains(t, text, "  development:\n    enabled: true")
	text = strings.Replace(text, "  development:\n    enabled: true", fixtures, 1)
	path := filepath.Join(dir, "control.dispatch.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

// upstream is the countable controlled upstream: it records every physical
// send it receives. It stands in for a provider; nothing here is a
// paid-provider claim.
type upstream struct {
	srv   *httptest.Server
	sends atomic.Int32
	mu    sync.Mutex
	calls []string
}

func newUpstream(t *testing.T) *upstream {
	u := &upstream{}
	u.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u.sends.Add(1)
		u.mu.Lock()
		u.calls = append(u.calls, r.Header.Get("X-Call-Id"))
		u.mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(u.srv.Close)
	return u
}

// send is the sender's one physical request after a permission: transparent
// retries are disabled, so a failed send is reported, never repeated.
func (u *upstream) send(t *testing.T, callID string) {
	t.Helper()
	req, err := http.NewRequest("POST", u.srv.URL+"/v1/model", strings.NewReader("{}"))
	require.NoError(t, err)
	req.Header.Set("X-Call-Id", callID)
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	resp.Body.Close()
}

func (e *env) fundedOperation(ctx context.Context, t *testing.T, ops controlv1.OperationServiceClient, exec controlv1.ExecutionServiceClient, run, name string, amount int64) (opID, attemptID string) {
	t.Helper()
	digest := digestOf(name)
	created, err := ops.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-" + name, ActorId: "user_a", RequestDigest: digest},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK,
		Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.NoError(t, err)
	opID = created.GetOperation().GetOperationId()
	opened, err := exec.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{
		Command:     &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-" + name + "-open", ActorId: "user_a", RequestDigest: digest},
		OperationId: opID, StepId: "local-check", VisitOrdinal: "0", ProfileId: "local-check-v1",
	})
	require.NoError(t, err)
	attemptID = opened.GetAttempt().GetAttemptId()
	if amount > 0 {
		conn := e.db()
		for _, level := range []string{"platform", "tenant", "actor"} {
			_, err := conn.Exec(ctx, "INSERT INTO allocations (allocation_id, pool_id, operation_id, currency, amount) VALUES ($1, $2, $3, 'USD', $4)",
				"alloc-"+run+"-"+name+"-"+level, "pool-"+run+"-"+level, opID, amount)
			require.NoError(t, err)
		}
	}
	return opID, attemptID
}

// TestDispatchAdmission runs P06 against two real Control processes on one
// database and one inventory directory with a countable controlled
// upstream: the sender (this test playing the Model Proxy) admits every call
// on both processes, sends exactly when told dispatch_allowed=true, and
// reports usage.
func TestDispatchAdmission(t *testing.T) {
	root := repoRoot(t)
	e := setupProcesses(t, true, "", dispatchControlConfig(t, root, t.TempDir()), true)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	up := newUpstream(t)
	opsA, execA := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	// Model dispatches are the Model Proxy's, tool dispatches the MCP's.
	dispA, dispB := controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalModelProxy)), controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddrB, principalModelProxy))
	toolA, toolB := controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalMCP)), controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddrB, principalMCP))
	both := []controlv1.DispatchServiceClient{dispA, dispB}

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

	// deadlines keeps one deadline per call: a reentry of a call carries the
	// deadline it was recorded with, as a real sender's digest covers it.
	var deadlinesMu sync.Mutex
	deadlines := map[string]*timestamppb.Timestamp{}
	deadlineOf := func(callID string) *timestamppb.Timestamp {
		deadlinesMu.Lock()
		defer deadlinesMu.Unlock()
		if d, ok := deadlines[callID]; ok {
			return d
		}
		deadlines[callID] = timestamppb.New(time.Now().Add(time.Minute))
		return deadlines[callID]
	}
	admitAs := func(c controlv1.DispatchServiceClient, opID, attemptID, callID, digest string, exposure int64, supersedes, evidence, provider, model string) (*controlv1.Admission, error) {
		req := &controlv1.AdmitModelRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-" + callID, ActorId: "proxy", RequestDigest: digest},
			Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
			// The owner is the verified caller (P0.2); call ids are run-unique.
			CallId: callID + "-" + run, Owner: principalModelProxy, RouteId: "fixture-route", Provider: provider, Model: model,
			MaxExposure: &controlv1.Money{Currency: "USD", Amount: fmt.Sprint(exposure)}, Deadline: deadlineOf(callID),
		}
		if supersedes != "" {
			superseded := supersedes + "-" + run
			req.SupersedesCallId, req.EvidenceRef = &superseded, &evidence
		}
		resp, err := c.AdmitModel(ctx, req)
		if err != nil {
			return nil, err
		}
		return resp.GetAdmission(), nil
	}
	admit := func(c controlv1.DispatchServiceClient, opID, attemptID, callID, digest string, exposure int64, supersedes, evidence string) (*controlv1.Admission, error) {
		return admitAs(c, opID, attemptID, callID, digest, exposure, supersedes, evidence, "fixture", "fixture-model")
	}
	// sendIfAllowed is the sender's contract: one physical send per
	// permission, then an idempotent observation.
	sendIfAllowed := func(c controlv1.DispatchServiceClient, a *controlv1.Admission) {
		if !a.GetDispatchAllowed() {
			return
		}
		up.send(t, a.GetDispatch().GetCallId())
		_, err := c.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: a.GetDispatch().GetDispatchId(), Source: "proxy", Sequence: "1",
			Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_SUCCEEDED, CumulativeUsage: &controlv1.Usage{InputUnits: "100", OutputUnits: "50", ReasoningUnits: "0", CachedInputUnits: "0"}, ObservedAt: timestamppb.Now()})
		require.NoError(t, err)
	}

	t.Run("the same call admitted concurrently on both processes is sent exactly once", func(t *testing.T) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "race", 100_000)
		digest := digestOf("race body")
		var wg sync.WaitGroup
		results := make([]*controlv1.Admission, 2)
		for i, c := range both {
			wg.Add(1)
			go func(i int, c controlv1.DispatchServiceClient) {
				defer wg.Done()
				a, err := admit(c, opID, attemptID, "call-race", digest, 1000, "", "")
				require.NoError(t, err)
				results[i] = a
				sendIfAllowed(c, a)
			}(i, c)
		}
		wg.Wait()
		allowed := 0
		for _, a := range results {
			if a.GetDispatchAllowed() {
				allowed++
			}
		}
		require.Equal(t, 1, allowed, "one first-use permission across the two processes")
		require.Equal(t, results[0].GetDispatch().GetDispatchId(), results[1].GetDispatch().GetDispatchId())
		require.Equal(t, int32(1), up.sends.Load(), "the upstream received exactly one physical send")
		got, err := dispB.GetDispatch(ctx, &controlv1.GetDispatchRequest{DispatchId: results[0].GetDispatch().GetDispatchId(), TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, controlv1.DispatchState_DISPATCH_STATE_OBSERVED, got.GetDispatch().GetState())
		var objects int
		require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM dispatches WHERE operation_id = $1", opID).Scan(&objects))
		require.Equal(t, 1, objects)
		_, err = os.Stat(filepath.Join(e.local, "inventory", "model-dispatch", results[0].GetDispatch().GetDispatchId()))
		require.NoError(t, err, "the obligation is inventoried before the permission")
		var charged int64
		require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(sum(amount), 0) FROM cost_entries WHERE operation_id = $1 AND kind IN ('actual', 'correction')", opID).Scan(&charged))
		require.Equal(t, int64(300+750), charged, "100 input at 3 and 50 output at 15 per unit")
	})

	t.Run("a lost first response is never restored by retries, queries, same-owner reentry or a process restart", func(t *testing.T) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "lost", 100_000)
		digest := digestOf("lost body")
		first, err := admit(dispA, opID, attemptID, "call-lost", digest, 1000, "", "")
		require.NoError(t, err)
		require.True(t, first.GetDispatchAllowed(), "this is the response the sender never receives; it does not send")
		for _, c := range []controlv1.DispatchServiceClient{dispA, dispB, dispA} {
			again, err := admit(c, opID, attemptID, "call-lost", digest, 1000, "", "")
			require.NoError(t, err)
			require.False(t, again.GetDispatchAllowed())
			sendIfAllowed(c, again)
		}
		got, err := dispB.GetDispatch(ctx, &controlv1.GetDispatchRequest{CallId: "call-lost-" + run, TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, controlv1.DispatchState_DISPATCH_STATE_AUTHORIZED, got.GetDispatch().GetState())
		_, err = admit(dispA, opID, attemptID, "call-lost", digestOf("changed body"), 1000, "", "")
		require.Equal(t, codes.Aborted, status.Code(err), "a changed digest under the same call conflicts")
		// The call identity bound to another operation, attempt, model or
		// exposure is another request: refused as a conflict on both
		// processes, never answered as the recorded call.
		otherOp, otherAttempt := e.fundedOperation(ctx, t, opsA, execA, run, "lost-other", 100_000)
		for name, probe := range map[string]func(c controlv1.DispatchServiceClient) (*controlv1.Admission, error){
			"operation": func(c controlv1.DispatchServiceClient) (*controlv1.Admission, error) {
				return admit(c, otherOp, otherAttempt, "call-lost", digest, 1000, "", "")
			},
			"attempt": func(c controlv1.DispatchServiceClient) (*controlv1.Admission, error) {
				return admit(c, opID, otherAttempt, "call-lost", digest, 1000, "", "")
			},
			"model": func(c controlv1.DispatchServiceClient) (*controlv1.Admission, error) {
				return admitAs(c, opID, attemptID, "call-lost", digest, 1000, "", "", "fixture", "other-model")
			},
			"exposure": func(c controlv1.DispatchServiceClient) (*controlv1.Admission, error) {
				return admit(c, opID, attemptID, "call-lost", digest, 999, "", "")
			},
		} {
			for i, c := range both {
				_, err := probe(c)
				require.Equal(t, codes.Aborted, status.Code(err), "%s changed on process %d: %v", name, i, err)
				require.Contains(t, status.Convert(err).Message(), "IDEMPOTENCY_CONFLICT")
			}
		}
		var reservedOther int64
		require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(max(reserved), 0) FROM allocations WHERE operation_id = $1", otherOp).Scan(&reservedOther))
		require.Zero(t, reservedOther, "the other operation reserved nothing for a call that is not its own")
		// Process A restarts: the permission is durable state, not memory.
		e.kill("control")
		e.start("control")
		e.waitTCP(e.controlAddr)
		connA2 := e.dialControl(e.controlAddr, principalModelProxy)
		again, err := admit(controlv1.NewDispatchServiceClient(connA2), opID, attemptID, "call-lost", digest, 1000, "", "")
		require.NoError(t, err)
		require.False(t, again.GetDispatchAllowed())
		require.Equal(t, int32(1), up.sends.Load(), "zero sends for the lost call: the earlier race scenario's single send is the only one")
	})

	t.Run("a definite outcome without usage is refused and keeps the exposure; explicit zero usage settles; a provider/model outside the trusted route is denied and nothing is sent", func(t *testing.T) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "settle", 100_000)
		a, err := admit(dispA, opID, attemptID, "call-settle", digestOf("settle"), 2000, "", "")
		require.NoError(t, err)
		require.True(t, a.GetDispatchAllowed())
		up.send(t, "call-settle-"+run)
		id := a.GetDispatch().GetDispatchId()
		var reserved int64
		for _, outcome := range []controlv1.DispatchOutcome{controlv1.DispatchOutcome_DISPATCH_OUTCOME_SUCCEEDED, controlv1.DispatchOutcome_DISPATCH_OUTCOME_FAILED, controlv1.DispatchOutcome_DISPATCH_OUTCOME_CANCELED} {
			_, err = dispB.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: id, Source: "proxy", Sequence: "1", Outcome: outcome, ObservedAt: timestamppb.Now()})
			require.Equal(t, codes.FailedPrecondition, status.Code(err), "%s without cumulative_usage: %v", outcome, err)
			require.Contains(t, status.Convert(err).Message(), "EFFECT_UNCERTAIN")
		}
		got, err := dispA.GetDispatch(ctx, &controlv1.GetDispatchRequest{DispatchId: id, TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, controlv1.DispatchState_DISPATCH_STATE_AUTHORIZED, got.GetDispatch().GetState(), "a refused report leaves the call as it is")
		require.NoError(t, conn.QueryRow(ctx, "SELECT max(reserved) FROM allocations WHERE operation_id = $1", opID).Scan(&reserved))
		require.Equal(t, int64(2000), reserved, "the exposure is not released as zero cost")
		var observations int
		require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM usage_observations WHERE dispatch_id = $1", id).Scan(&observations))
		require.Zero(t, observations, "a refused report is not recorded")
		unknown, err := dispA.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: id, Source: "proxy", Sequence: "1", Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_UNKNOWN, ObservedAt: timestamppb.Now()})
		require.NoError(t, err)
		require.Equal(t, controlv1.DispatchState_DISPATCH_STATE_UNKNOWN, unknown.GetDispatch().GetState())
		dup, err := dispB.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: id, Source: "proxy", Sequence: "1", Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_UNKNOWN, ObservedAt: timestamppb.Now()})
		require.NoError(t, err)
		require.True(t, dup.GetExisting(), "the same source and sequence is applied once on either process")
		var finance string
		require.NoError(t, conn.QueryRow(ctx, "SELECT finance_state FROM operations WHERE operation_id = $1", opID).Scan(&finance))
		require.Equal(t, "exposure_unknown", finance)
		settled, err := dispB.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: id, Source: "proxy", Sequence: "2", Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_SUCCEEDED,
			CumulativeUsage: &controlv1.Usage{InputUnits: "0", OutputUnits: "0", ReasoningUnits: "0", CachedInputUnits: "0"}, ObservedAt: timestamppb.Now()})
		require.NoError(t, err)
		require.Equal(t, controlv1.DispatchState_DISPATCH_STATE_OBSERVED, settled.GetDispatch().GetState(), "explicitly reported zero usage settles the call")
		require.NoError(t, conn.QueryRow(ctx, "SELECT max(reserved) FROM allocations WHERE operation_id = $1", opID).Scan(&reserved))
		require.Zero(t, reserved)
		var charged int64
		require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(sum(amount), 0) FROM cost_entries WHERE dispatch_id = $1 AND kind IN ('actual', 'correction')", id).Scan(&charged))
		require.Zero(t, charged)
		var reportedFlags []bool
		rows, err := conn.Query(ctx, "SELECT usage_reported FROM usage_observations WHERE dispatch_id = $1 ORDER BY sequence", id)
		require.NoError(t, err)
		for rows.Next() {
			var b bool
			require.NoError(t, rows.Scan(&b))
			reportedFlags = append(reportedFlags, b)
		}
		rows.Close()
		require.Equal(t, []bool{false, true}, reportedFlags, "the report without usage and the explicit zero are kept apart")

		before := up.sends.Load()
		denied, err := admitAs(dispB, opID, attemptID, "call-other-model", digestOf("other-model"), 10, "", "", "fixture", "other-model")
		require.NoError(t, err)
		require.False(t, denied.GetDispatchAllowed())
		require.Equal(t, "PROFILE_UNQUALIFIED", denied.GetDenialCode(), "a model the route's observation does not bind has no reviewed price")
		again, err := admitAs(dispA, opID, attemptID, "call-other-model", digestOf("other-model"), 10, "", "", "fixture", "other-model")
		require.NoError(t, err)
		require.Equal(t, "PROFILE_UNQUALIFIED", again.GetDenialCode(), "the denial is repeated under the call identity")
		_, err = admitAs(dispA, opID, attemptID, "call-empty-model", digestOf("empty-model"), 10, "", "", "fixture", "")
		require.Equal(t, codes.InvalidArgument, status.Code(err), "an incomplete binding is rejected before any handler")
		require.Equal(t, before, up.sends.Load(), "denials sent nothing")
	})

	t.Run("allocation races cannot exceed the allocation; duplicate and out-of-order usage cannot double-charge; overspend blocks new admission", func(t *testing.T) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "cap", 3500)
		var wg sync.WaitGroup
		var allowed atomic.Int32
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				id := fmt.Sprintf("call-cap-%d", i)
				a, err := admit(both[i%2], opID, attemptID, id, digestOf(id), 1000, "", "")
				require.NoError(t, err)
				if a.GetDispatchAllowed() {
					allowed.Add(1)
				} else {
					require.Equal(t, "BUDGET_EXHAUSTED", a.GetDenialCode())
				}
			}(i)
		}
		wg.Wait()
		require.Equal(t, int32(3), allowed.Load())
		var reserved int64
		require.NoError(t, conn.QueryRow(ctx, "SELECT max(reserved) FROM allocations WHERE operation_id = $1", opID).Scan(&reserved))
		require.Equal(t, int64(3000), reserved)

		opID, attemptID = e.fundedOperation(ctx, t, opsA, execA, run, "usage", 100_000)
		a, err := admit(dispA, opID, attemptID, "call-usage", digestOf("usage"), 10_000, "", "")
		require.NoError(t, err)
		require.True(t, a.GetDispatchAllowed())
		id := a.GetDispatch().GetDispatchId()
		observe := func(c controlv1.DispatchServiceClient, seq, in, out string) bool {
			resp, err := c.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: id, Source: "proxy", Sequence: seq, Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_SUCCEEDED,
				CumulativeUsage: &controlv1.Usage{InputUnits: in, OutputUnits: out, ReasoningUnits: "0", CachedInputUnits: "0"}, ObservedAt: timestamppb.Now()})
			require.NoError(t, err)
			return resp.GetExisting()
		}
		require.False(t, observe(dispA, "1", "100", "100"))
		require.True(t, observe(dispB, "1", "100", "100"), "duplicate")
		require.False(t, observe(dispB, "3", "300", "300"))
		require.False(t, observe(dispA, "2", "200", "200"), "out of order")
		var charged int64
		var kinds []string
		rows, err := conn.Query(ctx, "SELECT kind, amount FROM cost_entries WHERE dispatch_id = $1 AND kind <> 'estimate' ORDER BY created_at", id)
		require.NoError(t, err)
		for rows.Next() {
			var k string
			var v int64
			require.NoError(t, rows.Scan(&k, &v))
			kinds = append(kinds, k)
			charged += v
		}
		rows.Close()
		require.Equal(t, []string{"actual", "correction"}, kinds)
		require.Equal(t, int64(300*3+300*15), charged, "the running maximum is charged once")

		small, err := admit(dispB, opID, attemptID, "call-over", digestOf("over"), 10, "", "")
		require.NoError(t, err)
		require.True(t, small.GetDispatchAllowed())
		_, err = dispA.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: small.GetDispatch().GetDispatchId(), Source: "proxy", Sequence: "1", Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_SUCCEEDED,
			CumulativeUsage: &controlv1.Usage{InputUnits: "0", OutputUnits: "1000", ReasoningUnits: "0", CachedInputUnits: "0"}, ObservedAt: timestamppb.Now()})
		require.NoError(t, err)
		require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(sum(amount), 0) FROM cost_entries WHERE dispatch_id = $1 AND kind IN ('actual', 'correction')", small.GetDispatch().GetDispatchId()).Scan(&charged))
		require.Equal(t, int64(15000), charged, "the overspend is retained in full")
		blocked, err := admit(dispB, opID, attemptID, "call-after-over", digestOf("after"), 10, "", "")
		require.NoError(t, err)
		require.False(t, blocked.GetDispatchAllowed())
		require.Equal(t, "BUDGET_EXHAUSTED", blocked.GetDenialCode())
	})

	t.Run("cancellation fences admission; unknown exposure is retained until independent not-sent evidence; a replacement is freshly admitted and sent once", func(t *testing.T) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "unknown", 100_000)
		a, err := admit(dispA, opID, attemptID, "call-unk", digestOf("unk"), 2000, "", "")
		require.NoError(t, err)
		require.True(t, a.GetDispatchAllowed())
		// The send outcome is unknown (the sender lost the upstream answer).
		_, err = dispB.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: a.GetDispatch().GetDispatchId(), Source: "proxy", Sequence: "1", Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_UNKNOWN, ObservedAt: timestamppb.Now()})
		require.NoError(t, err)
		var reserved int64
		var finance string
		require.NoError(t, conn.QueryRow(ctx, "SELECT max(reserved) FROM allocations WHERE operation_id = $1", opID).Scan(&reserved))
		require.NoError(t, conn.QueryRow(ctx, "SELECT finance_state FROM operations WHERE operation_id = $1", opID).Scan(&finance))
		require.Equal(t, int64(2000), reserved, "unknown exposure is retained")
		require.Equal(t, "exposure_unknown", finance)

		id := a.GetDispatch().GetDispatchId()
		cmdID := &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "confirm-" + run, ActorId: "operator", RequestDigest: digestOf("confirm")}
		_, err = dispA.ConfirmNotSent(ctx, &controlv1.ConfirmNotSentRequest{Command: cmdID, DispatchId: id, EvidenceRef: "not-sent/" + id, EvidenceDigest: digestOf("nothing")})
		require.Equal(t, codes.FailedPrecondition, status.Code(err), "a caller assertion without an object is insufficient")
		// The trusted supervisor seals the attestation into the shared inventory.
		body, _ := json.Marshal(map[string]string{"class": "not-sent", "dispatchId": id, "callId": "call-unk-" + run, "owner": principalModelProxy, "issuer": "integration-supervisor", "sealedAt": time.Now().UTC().Format(time.RFC3339)})
		require.NoError(t, os.MkdirAll(filepath.Join(e.local, "inventory", "not-sent"), 0o750))
		require.NoError(t, os.WriteFile(filepath.Join(e.local, "inventory", "not-sent", id), body, 0o640))
		evidenceDigest := fmt.Sprintf("sha256:%x", sha256.Sum256(body))
		confirmed, err := dispB.ConfirmNotSent(ctx, &controlv1.ConfirmNotSentRequest{Command: cmdID, DispatchId: id, EvidenceRef: "not-sent/" + id, EvidenceDigest: evidenceDigest})
		require.NoError(t, err)
		require.Equal(t, controlv1.DispatchState_DISPATCH_STATE_CONFIRMED_NOT_SENT, confirmed.GetDispatch().GetState())
		require.NoError(t, conn.QueryRow(ctx, "SELECT max(reserved) FROM allocations WHERE operation_id = $1", opID).Scan(&reserved))
		require.Zero(t, reserved)
		before := up.sends.Load()
		refused, err := admit(dispA, opID, attemptID, "call-unk-2", digestOf("unk2"), 2000, "call-unk", "")
		require.NoError(t, err)
		require.Equal(t, "STALE_EXECUTION", refused.GetDenialCode(), "a replacement without the confirming evidence")
		replacement, err := admit(dispB, opID, attemptID, "call-unk-3", digestOf("unk3"), 2000, "call-unk", "not-sent/"+id)
		require.NoError(t, err)
		require.True(t, replacement.GetDispatchAllowed())
		sendIfAllowed(dispB, replacement)
		again, err := admit(dispA, opID, attemptID, "call-unk-3", digestOf("unk3"), 2000, "call-unk", "not-sent/"+id)
		require.NoError(t, err)
		require.False(t, again.GetDispatchAllowed())
		require.Equal(t, before+1, up.sends.Load(), "the replacement was sent exactly once")

		// A cancel fence installed by the operator denies every later admission.
		revision := e.settledRevision(ctx, t, conn, opID)
		_, err = opsA.SubmitCommand(ctx, &controlv1.SubmitCommandRequest{Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cancel-" + run, ActorId: "user_a", RequestDigest: digestOf("cancel")},
			Scope: &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"}, OperationId: opID, Kind: controlv1.CommandKind_COMMAND_KIND_CANCEL, ExpectedRevision: revision})
		require.NoError(t, err)
		fenced, err := admit(dispB, opID, attemptID, "call-after-cancel", digestOf("after-cancel"), 10, "", "")
		require.NoError(t, err)
		require.Equal(t, "STALE_EXECUTION", fenced.GetDenialCode())
		// Settle the attempt so the operation does not linger in the dev database.
		_, err = execA.CloseAttempt(ctx, &controlv1.CloseAttemptRequest{Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "close-" + run, ActorId: "user_a", RequestDigest: digestOf("close")},
			AttemptId: attemptID, Outcome: controlv1.AttemptOutcome_ATTEMPT_OUTCOME_CANCELED, Cleanup: controlv1.CleanupState_CLEANUP_STATE_NOT_REQUIRED})
		require.NoError(t, err)
	})

	t.Run("missing price, authorization, allocation or grant denies; nothing is sent", func(t *testing.T) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "deny", 100_000)
		before := up.sends.Load()
		resp, err := dispA.AdmitModel(ctx, &controlv1.AdmitModelRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-noprice", ActorId: "proxy", RequestDigest: digestOf("noprice")},
			Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
			CallId:  "call-noprice-" + run, Owner: principalModelProxy, RouteId: "unpriced-route", Provider: "fixture", Model: "other",
			MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10"}, Deadline: timestamppb.New(time.Now().Add(time.Minute)),
		})
		require.NoError(t, err)
		require.Equal(t, "PROFILE_UNQUALIFIED", resp.GetAdmission().GetDenialCode())
		unfundedOp, unfundedAttempt := e.fundedOperation(ctx, t, opsA, execA, run, "unfunded", 0)
		a, err := admit(dispB, unfundedOp, unfundedAttempt, "call-unfunded", digestOf("unfunded"), 10, "", "")
		require.NoError(t, err)
		require.Equal(t, "BUDGET_EXHAUSTED", a.GetDenialCode())
		tool, err := toolA.AdmitTool(ctx, &controlv1.AdmitToolRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-tool", ActorId: "mcp", RequestDigest: digestOf("tool")},
			Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
			CallId:  "call-tool-" + run, Owner: principalMCP, GrantId: "grant-" + run, GrantRevision: "1", ServerId: "srv", Method: "read", ArgumentDigest: digestOf("args"),
			MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10"}, Deadline: timestamppb.New(time.Now().Add(time.Minute)),
		})
		require.NoError(t, err)
		require.Equal(t, "FORBIDDEN", tool.GetAdmission().GetDenialCode(), "no registered grant")
		_, err = conn.Exec(ctx, "INSERT INTO grant_policies (grant_id, grant_revision, tenant_id, policy_digest, server_id, methods, policy_epoch, receipt_id, revocation_state, register_command_id, register_request_digest) VALUES ($1, 1, 'tenant_a', $2, 'srv', ARRAY['read'], 1, $3, 'none', $4, $2)", "grant-"+run, digestOf("policy"), "rcpt-"+run, "reg-"+run)
		require.NoError(t, err)
		tool2, err := toolB.AdmitTool(ctx, &controlv1.AdmitToolRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-tool2", ActorId: "mcp", RequestDigest: digestOf("tool2")},
			Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
			CallId:  "call-tool-2-" + run, Owner: principalMCP, GrantId: "grant-" + run, GrantRevision: "1", ServerId: "srv", Method: "read", ArgumentDigest: digestOf("args"),
			MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10"}, Deadline: timestamppb.New(time.Now().Add(time.Minute)),
		})
		require.NoError(t, err)
		require.True(t, tool2.GetAdmission().GetDispatchAllowed(), "a registered, unrevoked grant admits the priced method once")
		_, err = os.Stat(filepath.Join(e.local, "inventory", "tool-dispatch", tool2.GetAdmission().GetDispatch().GetDispatchId()))
		require.NoError(t, err)
		require.Equal(t, before, up.sends.Load(), "denials sent nothing")
	})

	// P0.2 AC1/AC4 on the foundation: the dispatch owner is the verified
	// caller, an owner reads and reports only its own dispatches, and every
	// read is bound to the tenant it acts for.
	t.Run("the owner and the tenant of a dispatch come from the caller", func(t *testing.T) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "derived", 100_000)
		admitOwner := func(c controlv1.DispatchServiceClient, callID, owner string) (*controlv1.AdmitModelResponse, error) {
			return c.AdmitModel(ctx, &controlv1.AdmitModelRequest{
				Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-" + callID, ActorId: "proxy", RequestDigest: digestOf(callID)},
				Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
				CallId:  callID + "-" + run, Owner: owner, RouteId: "fixture-route", Provider: "fixture", Model: "fixture-model",
				MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10"}, Deadline: timestamppb.New(time.Now().Add(time.Minute)),
			})
		}
		_, err := admitOwner(dispA, "call-forged-owner", principalMCP)
		require.Equal(t, codes.PermissionDenied, status.Code(err), "the proxy may not admit under the MCP owner: %v", err)
		_, err = admitOwner(controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalWorkflow)), "call-workflow", principalModelProxy)
		require.Equal(t, codes.PermissionDenied, status.Code(err), "the Workflow may not admit as the proxy: %v", err)
		own, err := admitOwner(dispB, "call-own", principalModelProxy)
		require.NoError(t, err)
		require.Equal(t, principalModelProxy, own.GetAdmission().GetDispatch().GetOwner())
		id := own.GetAdmission().GetDispatch().GetDispatchId()
		read := func(c controlv1.DispatchServiceClient, tenant string) error {
			_, err := c.GetDispatch(ctx, &controlv1.GetDispatchRequest{DispatchId: id, TenantId: tenant})
			return err
		}
		require.NoError(t, read(dispA, "tenant_a"))
		require.Equal(t, codes.NotFound, status.Code(read(dispA, "tenant_b")), "a dispatch of another tenant is not found")
		require.Equal(t, codes.NotFound, status.Code(read(toolA, "tenant_a")), "MCP does not read the proxy's dispatch")
		_, err = toolB.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: id, Source: "mcp", Sequence: "1", Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_UNKNOWN, ObservedAt: timestamppb.Now()})
		require.Equal(t, codes.NotFound, status.Code(err), "MCP does not report on the proxy's dispatch: %v", err)
	})
}

func strPtr(s string) *string { return &s }

// settledRevision returns the operation's revision once Control's relay has
// applied the start intent that accepting a LocalCheck operation commits.
// The relay marks that intent asynchronously (relay:started, a revision of
// its own) within one relay interval of the intake, and these scenarios run
// their whole sequence inside that interval; a tracked command carrying a
// revision read before the mark would race it and be refused with
// REVISION_CONFLICT. Nothing else moves a funding operation of these
// scenarios: no Workflow worker runs, and a settled intent is not marked
// twice.
func (e *env) settledRevision(ctx context.Context, t *testing.T, conn *pgx.Conn, opID string) string {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for {
		var revision, relay string
		require.NoError(t, conn.QueryRow(ctx, "SELECT revision, relay_state FROM operations WHERE operation_id = $1", opID).Scan(&revision, &relay))
		if relay != "pending" {
			return revision
		}
		require.True(t, time.Now().Before(deadline), "the relay did not apply the start intent of %s", opID)
		time.Sleep(50 * time.Millisecond)
	}
}
