//go:build integration

package integration

import (
	"context"
	"fmt"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
)

// TestDispatchRaceOnTheS3Inventory is M1-13's concurrency scenario (P3.4,
// TEST-02): two Control processes over one database whose obligation
// inventory is the foundation's S3 store (not a shared directory), raced
// by 2 and 8 concurrent admissions of one call identity spread over both:
// exactly one dispatchAllowed=true, one dispatch row, one physical send.
// DEVELOPMENT_ONLY placement: host processes and the foundation's MinIO,
// not two replicas behind a Service on the qualified object store (G-14).
func TestDispatchRaceOnTheS3Inventory(t *testing.T) {
	var s3Env []string
	for _, n := range []string{"ANVILKIT_DEV_ARTIFACTS_ENDPOINT", "ANVILKIT_DEV_INVENTORY_BUCKET", "ANVILKIT_DEV_INVENTORY_ACCESS_KEY_ID", "ANVILKIT_DEV_INVENTORY_SECRET_ACCESS_KEY"} {
		if os.Getenv(n) == "" {
			skipMissing(t, n, "not set; source .local/dev/env.sh after deploy/dev/up.sh")
		}
	}
	s3Env = append(s3Env,
		"ANVILKIT_CONTROL_INVENTORY_S3_ENDPOINT="+os.Getenv("ANVILKIT_DEV_ARTIFACTS_ENDPOINT"),
		"ANVILKIT_CONTROL_INVENTORY_S3_BUCKET="+os.Getenv("ANVILKIT_DEV_INVENTORY_BUCKET"),
		"ANVILKIT_CONTROL_INVENTORY_S3_ACCESS_KEY_ID="+os.Getenv("ANVILKIT_DEV_INVENTORY_ACCESS_KEY_ID"),
		"ANVILKIT_CONTROL_INVENTORY_S3_SECRET_ACCESS_KEY="+os.Getenv("ANVILKIT_DEV_INVENTORY_SECRET_ACCESS_KEY"))
	root := repoRoot(t)
	dir := t.TempDir()
	cfg := dispatchControlConfig(t, root, dir)
	text, err := os.ReadFile(cfg)
	require.NoError(t, err)
	require.Contains(t, string(text), "inventory:\n  backend: filesystem")
	require.NoError(t, os.WriteFile(cfg, []byte(strings.Replace(string(text), "inventory:\n  backend: filesystem", "inventory:\n  backend: s3", 1)), 0o600))
	e := setupProcessesEnv(t, true, "", cfg, true, s3Env)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	up := newUpstream(t)
	opsA, execA := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	both := []controlv1.DispatchServiceClient{
		controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalModelProxy)),
		controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddrB, principalModelProxy)),
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
	t.Cleanup(func() {
		_, _ = conn.Exec(context.Background(), "UPDATE budget_pools SET period_end = period_start + interval '1 second' WHERE pool_id LIKE $1", "pool-"+run+"-%")
	})

	race := func(t *testing.T, ways int) {
		opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, fmt.Sprintf("race%d", ways), 100_000)
		callID := fmt.Sprintf("call-race%d-%s", ways, run)
		deadline := timestamppb.New(time.Now().Add(time.Minute))
		before := up.sends.Load()
		var wg sync.WaitGroup
		results := make([]*controlv1.Admission, ways)
		for i := 0; i < ways; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				c := both[i%2]
				resp, err := c.AdmitModel(ctx, &controlv1.AdmitModelRequest{
					Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + callID, ActorId: "proxy", RequestDigest: digestOf("race body")},
					Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
					CallId:  callID, Owner: principalModelProxy, RouteId: "fixture-route", Provider: "fixture", Model: "fixture-model",
					MaxExposure: &controlv1.Money{Currency: "USD", Amount: "1000"}, Deadline: deadline,
				})
				require.NoError(t, err)
				results[i] = resp.GetAdmission()
				if results[i].GetDispatchAllowed() {
					up.send(t, callID)
				}
			}(i)
		}
		wg.Wait()
		allowed := 0
		for _, a := range results {
			if a.GetDispatchAllowed() {
				allowed++
			}
			require.Equal(t, results[0].GetDispatch().GetDispatchId(), a.GetDispatch().GetDispatchId(), "every racer answers the one dispatch")
		}
		require.Equal(t, 1, allowed, "one first-use permission across %d racers on two processes", ways)
		require.Equal(t, before+1, up.sends.Load(), "the upstream received exactly one physical send")
		var rows int
		require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM dispatches WHERE operation_id = $1", opID).Scan(&rows))
		require.Equal(t, 1, rows)
	}
	t.Run("a 2-way race over two Control processes on the S3 inventory admits one send", func(t *testing.T) { race(t, 2) })
	t.Run("an 8-way race over two Control processes on the S3 inventory admits one send", func(t *testing.T) { race(t, 8) })
}
