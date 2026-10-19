//go:build integration

package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"
)

// controlRelease is the Control release of the scenario (the repository's
// committed main, 2026-10-14), whose newest migration is
// 00018_append_only_privileges (it revokes the app role's ledger rewrites).
// Image N+1 is that release; image N is the same tree with its newest
// migration removed from the embedded set. No buildable historical commit
// can serve as image N: ddc688d, which made a replica accept a schema ahead
// of its own, does not build alone, and 2d39e01 builds but withholds
// readiness on a newer schema (a Kubernetes rollout would take it out of
// its Service mid-migration). Both are built from the Control repository's
// own history (git archive into a temporary directory, no worktree) with
// the module requirements they were committed with.
const controlRelease = "29ddee8caa5041348b23430d588a1b7067083aea"

// buildControlAt builds the Control service and its migration command of
// one recorded commit, without its newest migration when dropNewest is set,
// and returns their paths.
func buildControlAt(t *testing.T, rev string, dropNewest bool) (service, migration string) {
	t.Helper()
	src, bin := t.TempDir(), t.TempDir()
	archive := exec.Command("git", "-C", filepath.Join(repoRoot(t), serviceDirs["anvilkit-agent-control"]), "archive", "--format=tar", rev)
	extract := exec.Command("tar", "-x", "-C", src)
	var stderr bytes.Buffer
	archive.Stderr = &stderr
	pipe, err := archive.StdoutPipe()
	require.NoError(t, err)
	extract.Stdin = pipe
	require.NoError(t, archive.Start())
	out, err := extract.CombinedOutput()
	require.NoError(t, err, string(out))
	require.NoError(t, archive.Wait(), stderr.String())
	if dropNewest {
		migrations, err := filepath.Glob(filepath.Join(src, "internal", "migrate", "sql", "*.sql"))
		require.NoError(t, err)
		require.NotEmpty(t, migrations)
		require.NoError(t, os.Remove(migrations[len(migrations)-1]))
	}
	for _, cmd := range []string{"anvilkit-agent-control", "anvilkit-migration"} {
		build := exec.Command("go", "build", "-o", filepath.Join(bin, cmd), "./cmd/"+cmd)
		build.Dir = src
		build.Env = append(os.Environ(), "GOWORK=off", "GOFLAGS=-mod=readonly -buildvcs=false")
		out, err := build.CombinedOutput()
		require.NoError(t, err, "build %s at %s: %s", cmd, rev, out)
	}
	return filepath.Join(bin, "anvilkit-agent-control"), filepath.Join(bin, "anvilkit-migration")
}

// trafficCounts are what the LocalCheck traffic of the scenario observed.
type trafficCounts struct {
	mu       sync.Mutex
	statuses map[int]int
	errors   []string
	ops      []string
}

func (c *trafficCounts) record(status int, opID string, err error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err != nil {
		c.errors = append(c.errors, err.Error())
		return
	}
	c.statuses[status]++
	if opID != "" {
		c.ops = append(c.ops, opID)
	}
}

// TestMigrationUnderTraffic is TEST-05 (plan 0004 P3.4 AC4): migration N+1
// applied to a populated database while image N serves LocalCheck traffic,
// then mixed versions (one Control process at N, one at N+1, on the
// migrated schema) and a Workflow worker restart while runs are in flight.
// It requires no failed admission, every run succeeding, the migration's
// lock waits within the migrator's own bound (no lock_timeout retry
// exhausted, each reported), and no non-determinism in the worker's log.
// The database, Temporal and object store are the scenario's own; the
// LocalCheck Jobs run on the development foundation's kind cluster.
func TestMigrationUnderTraffic(t *testing.T) {
	controlN, migrationN := buildControlAt(t, controlRelease, true)
	controlN1, migrationN1 := buildControlAt(t, controlRelease, false)
	var migratorDSN string
	isolatedControlDatabaseMigratedBy(t, func(dsn string) *exec.Cmd {
		migratorDSN = dsn
		cmd := exec.Command(migrationN)
		cmd.Env = append(os.Environ(), "ANVILKIT_MIGRATION_DSN="+dsn, "ANVILKIT_MIGRATION_DEVELOPMENT=true")
		return cmd
	})
	t.Setenv("ANVILKIT_INTEGRATION_CONTROL_BINARY", controlN)
	e := setupProcesses(t, true, "", "", false)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	run := fmt.Sprint(time.Now().UnixNano())

	conn, err := pgx.Connect(ctx, e.controlDSN)
	require.NoError(t, err)
	defer conn.Close(context.Background())
	schema := func() int64 {
		var v int64
		require.NoError(t, conn.QueryRow(ctx, "SELECT max(version_id) FROM goose_db_version WHERE is_applied").Scan(&v))
		return v
	}
	require.Equal(t, int64(17), schema(), "image N's migrator brought the database to N")

	// A populated snapshot: completed operations before the migration.
	for i := range 3 {
		code, out := e.call("POST", "/api/v1/operations", tokenA, createBody(fmt.Sprintf("cmd-%s-before-%d", run, i)))
		require.Equal(t, 202, code, "%v", out)
		frames := e.stream(ctx, out["operationId"].(string), tokenA, "", terminal)
		require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))
	}

	// LocalCheck traffic for the whole scenario: one operation every 2 s.
	counts := &trafficCounts{statuses: map[int]int{}}
	stop := make(chan struct{})
	var traffic sync.WaitGroup
	traffic.Add(1)
	go func() {
		defer traffic.Done()
		client := &http.Client{Timeout: 30 * time.Second}
		for i := 0; ; i++ {
			select {
			case <-stop:
				return
			case <-time.After(2 * time.Second):
			}
			req, err := http.NewRequestWithContext(ctx, "POST", e.apiURL+"/api/v1/operations", strings.NewReader(createBody(fmt.Sprintf("cmd-%s-traffic-%d", run, i))))
			if err != nil {
				counts.record(0, "", err)
				continue
			}
			req.Header.Set("Authorization", "Bearer "+tokenA)
			req.Header.Set("Content-Type", "application/json")
			resp, err := client.Do(req)
			if err != nil {
				counts.record(0, "", err)
				continue
			}
			var out map[string]any
			_ = json.NewDecoder(resp.Body).Decode(&out)
			resp.Body.Close()
			id, _ := out["operationId"].(string)
			counts.record(resp.StatusCode, id, nil)
		}
	}()
	stopTraffic := func() {
		select {
		case <-stop:
		default:
			close(stop)
		}
		traffic.Wait()
	}
	defer stopTraffic()
	time.Sleep(6 * time.Second)

	// Migration N+1 while image N serves: the N+1
	// release's migrator, its per-session lock_timeout and bounded retries
	// (P3.7).
	migrate := exec.CommandContext(ctx, migrationN1)
	migrate.Env = append(os.Environ(), "ANVILKIT_MIGRATION_DSN="+migratorDSN, "ANVILKIT_MIGRATION_DEVELOPMENT=true")
	started := time.Now()
	out, err := migrate.CombinedOutput()
	took := time.Since(started)
	require.NoError(t, err, "migration N+1 under traffic: %s", out)
	require.Contains(t, string(out), "domain=control version=18")
	retries := strings.Count(string(out), "lock_timeout")
	t.Logf("migration N+1 under traffic: %s, %d lock_timeout retries", took, retries)
	require.Zero(t, retries, "a LocalCheck transaction held a lock past the migrator's 5 s lock_timeout: %s", out)
	require.Equal(t, int64(18), schema())
	// Image N keeps serving and stays ready on the schema ahead of its own.
	ready := func(name string) int {
		resp, err := http.Get("http://" + e.health[name] + "/readyz")
		if err != nil {
			return 0
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	require.Eventually(t, func() bool { return ready("control") == 200 && ready("control-b") == 200 }, 30*time.Second, time.Second, "image N stays ready on schema N+1")

	// Mixed versions on the migrated schema: the Workflow's Control process
	// is rolled to image N+1 while image N keeps serving the API, then the
	// worker restarts with runs in flight.
	time.Sleep(6 * time.Second)
	e.stop("control-b")
	e.binaries["control-b"] = controlN1
	e.start("control-b")
	e.waitTCP(e.controlAddrB)
	require.Eventually(t, func() bool { return ready("control-b") == 200 }, 30*time.Second, time.Second, "image N+1 is ready")
	time.Sleep(6 * time.Second)
	e.stop("workflow")
	e.start("workflow")
	time.Sleep(10 * time.Second)
	stopTraffic()

	counts.mu.Lock()
	statuses, errs, ops := counts.statuses, counts.errors, append([]string(nil), counts.ops...)
	counts.mu.Unlock()
	t.Logf("traffic: %d admitted, statuses %v, transport errors %d", len(ops), statuses, len(errs))
	require.Empty(t, errs, "no request of the traffic failed in transport")
	require.Equal(t, map[int]int{202: len(ops)}, statuses, "no failed admission")
	require.GreaterOrEqual(t, len(ops), 10, "the traffic spanned the migration and the rollout")

	// Every run of the traffic completes successfully across the migration,
	// the mixed versions and the worker restart.
	for _, id := range ops {
		var last map[string]any
		for until := time.Now().Add(5 * time.Minute); time.Now().Before(until); time.Sleep(time.Second) {
			code, view := e.call("GET", "/api/v1/operations/"+id, tokenA, "")
			require.Equal(t, 200, code, "%v", view)
			last = view
			if l := view["lifecycle"]; l == "succeeded" || l == "failed" || l == "canceled" {
				break
			}
		}
		require.Equal(t, "succeeded", last["lifecycle"], "operation %s: %v", id, last)
	}
	logs, err := os.ReadFile(filepath.Join(e.local, "workflow.log"))
	require.NoError(t, err)
	require.NotContains(t, strings.ToLower(string(logs)), "nondeterministic", "no non-determinism across the worker restart")
}
