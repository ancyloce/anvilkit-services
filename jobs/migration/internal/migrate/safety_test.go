package migrate

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"testing/fstest"
	"time"

	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
)

// safetyDBs starts a disposable PostgreSQL with both domains' roles and
// databases and returns each domain's migrator DSN.
func safetyDBs(t *testing.T) (context.Context, map[string]string) {
	t.Helper()
	if os.Getenv("ANVILKIT_SKIP_DOCKER_TESTS") != "" {
		t.Skip("ANVILKIT_SKIP_DOCKER_TESTS set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	t.Cleanup(cancel)
	pg, err := postgres.Run(ctx, "postgres:17-alpine",
		postgres.WithUsername("postgres"), postgres.WithPassword("postgres"), postgres.WithDatabase("postgres"),
		postgres.BasicWaitStrategies())
	require.NoError(t, err)
	testcontainers.CleanupContainer(t, pg)
	adminDSN, err := pg.ConnectionString(ctx, "sslmode=disable")
	require.NoError(t, err)
	admin, err := sql.Open("pgx", adminDSN)
	require.NoError(t, err)
	defer admin.Close()
	host, err := pg.Host(ctx)
	require.NoError(t, err)
	port, err := pg.MappedPort(ctx, "5432/tcp")
	require.NoError(t, err)
	dsns := map[string]string{}
	for _, domain := range Domains {
		stmts := []string{
			fmt.Sprintf("CREATE ROLE anvilkit_%s_app LOGIN PASSWORD 'app'", domain),
			fmt.Sprintf("CREATE ROLE anvilkit_%s_migrator LOGIN PASSWORD 'migrator'", domain),
			fmt.Sprintf("CREATE ROLE anvilkit_%s_relay LOGIN PASSWORD 'relay'", domain),
			fmt.Sprintf("CREATE DATABASE anvilkit_%s OWNER anvilkit_%s_migrator", domain, domain),
		}
		if domain == "knowledge" {
			stmts = append(stmts, "CREATE ROLE anvilkit_knowledge_forwarder LOGIN PASSWORD 'forwarder'",
				"CREATE ROLE anvilkit_knowledge_store_migrator LOGIN PASSWORD 'store'",
				"CREATE ROLE anvilkit_knowledge_cleanup LOGIN PASSWORD 'cleanup'")
		}
		for _, stmt := range stmts {
			_, err := admin.ExecContext(ctx, stmt)
			require.NoError(t, err, stmt)
		}
		dsns[domain] = fmt.Sprintf("postgres://anvilkit_%s_migrator:migrator@%s:%s/anvilkit_%s?sslmode=disable", domain, host, port.Port(), domain)
	}
	return ctx, dsns
}

// P3.7 AC4: per domain, two runners started at once serialize on the
// domain's advisory lock - both end at one version, every migration
// recorded once - and a held lock is waited on only for LockWait.
func TestConcurrentRunnersSerializeOnTheAdvisoryLock(t *testing.T) {
	ctx, dsns := safetyDBs(t)
	for _, domain := range Domains {
		t.Run(domain, func(t *testing.T) {
			var wg sync.WaitGroup
			versions := make([]int64, 2)
			errs := make([]error, 2)
			for i := range 2 {
				wg.Add(1)
				go func() {
					defer wg.Done()
					db, err := Open(dsns[domain])
					if err != nil {
						errs[i] = err
						return
					}
					defer db.Close()
					versions[i], errs[i] = Up(ctx, db, domain)
				}()
			}
			wg.Wait()
			require.NoError(t, errs[0])
			require.NoError(t, errs[1])
			require.Equal(t, versions[0], versions[1], "both runners end at one version")
			db, err := Open(dsns[domain])
			require.NoError(t, err)
			defer db.Close()
			var doubled int
			require.NoError(t, db.QueryRowContext(ctx, "SELECT count(*) FROM (SELECT version_id FROM goose_db_version WHERE is_applied GROUP BY version_id HAVING count(*) > 1) d").Scan(&doubled))
			require.Zero(t, doubled, "no migration applied twice")

			holder, err := db.Conn(ctx)
			require.NoError(t, err)
			defer holder.Close()
			_, err = holder.ExecContext(ctx, "SELECT pg_advisory_lock($1)", lockIDs[domain])
			require.NoError(t, err)
			o := DefaultOptions()
			o.LockWait = 2 * time.Second
			_, err = UpWith(ctx, db, domain, o)
			require.Error(t, err, "a held schema lock is not waited on past LockWait")
			_, err = holder.ExecContext(ctx, "SELECT pg_advisory_unlock($1)", lockIDs[domain])
			require.NoError(t, err)
			_, err = UpWith(ctx, db, domain, o)
			require.NoError(t, err)
		})
	}
}

// P3.7 AC4: a migration blocked by a long transaction fails at lock_timeout
// and is retried; readers never queue behind its pending exclusive lock for
// longer than the timeout; once the transaction ends it applies.
func TestALockTimeoutIsRetriedNotStacked(t *testing.T) {
	ctx, dsns := safetyDBs(t)
	dsn := dsns["mcp"]
	first := fstest.MapFS{"00001_init.sql": {Data: []byte("-- +goose Up\nCREATE TABLE t (x int);\nINSERT INTO t VALUES (1);\n-- +goose Down\nDROP TABLE t;\n")}}
	second := fstest.MapFS{"00001_init.sql": first["00001_init.sql"],
		"00002_add.sql": {Data: []byte("-- +goose Up\nALTER TABLE t ADD COLUMN y int;\n-- +goose Down\nALTER TABLE t DROP COLUMN y;\n")}}
	o := Options{LockTimeout: 300 * time.Millisecond, StatementTimeout: time.Minute, Retries: 20, RetryDelay: 100 * time.Millisecond, LockWait: 30 * time.Second}
	db, err := OpenWith(dsn, o)
	require.NoError(t, err)
	defer db.Close()
	v, err := upFS(ctx, db, "mcp", first, o)
	require.NoError(t, err)
	require.Equal(t, int64(1), v)

	plain, err := sql.Open("pgx", dsn)
	require.NoError(t, err)
	defer plain.Close()
	long, err := plain.BeginTx(ctx, nil)
	require.NoError(t, err)
	_, err = long.ExecContext(ctx, "SELECT * FROM t")
	require.NoError(t, err)

	var retries atomic.Int32
	o.OnRetry = func(int, error) { retries.Add(1) }
	done := make(chan error, 1)
	go func() {
		_, err := upFS(ctx, db, "mcp", second, o)
		done <- err
	}()
	require.Eventually(t, func() bool { return retries.Load() >= 1 }, 20*time.Second, 50*time.Millisecond, "the blocked ALTER failed at lock_timeout and was retried")
	for range 5 {
		readStart := time.Now()
		var n int
		require.NoError(t, plain.QueryRowContext(ctx, "SELECT count(*) FROM t").Scan(&n))
		require.Less(t, time.Since(readStart), 2*time.Second, "a reader stacked behind the pending migration")
	}
	require.NoError(t, long.Commit())
	select {
	case err := <-done:
		require.NoError(t, err)
	case <-time.After(30 * time.Second):
		t.Fatal("the migration did not apply after the long transaction ended")
	}
}
