//go:build integration

package integration

import (
	"context"
	"fmt"
	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// Disposable database used by the counting-upstream regression. Control's
// Temporal frontend is also private to that run; no everyday dependencies are used.
func isolatedControlDatabase(t *testing.T) {
	t.Helper()
	isolatedControlDatabaseMigratedBy(t, func(dsn string) *exec.Cmd {
		cmd := exec.Command("go", "run", "./cmd/anvilkit-migration")
		cmd.Dir = filepath.Join(repoRoot(t), serviceDirs["anvilkit-agent-control"])
		// The scenario's own PostgreSQL is plaintext (DEVELOPMENT_ONLY, P0.6).
		cmd.Env = append(os.Environ(), "ANVILKIT_MIGRATION_DSN="+dsn, "ANVILKIT_MIGRATION_DEVELOPMENT=true")
		return cmd
	})
}

// isolatedControlDatabaseMigratedBy is isolatedControlDatabase with the
// migration command the scenario chooses (an earlier image's migrator for
// the migration-under-traffic case, TEST-05), given the migrator's DSN.
func isolatedControlDatabaseMigratedBy(t *testing.T, migration func(migratorDSN string) *exec.Cmd) {
	t.Helper()
	ctx := context.Background()
	pgc, err := postgres.Run(ctx, "postgres:17-alpine", postgres.WithUsername("postgres"), postgres.WithPassword("disposable"), postgres.WithDatabase("postgres"), postgres.BasicWaitStrategies())
	require.NoError(t, err)
	testcontainers.CleanupContainer(t, pgc)
	dsn, err := pgc.ConnectionString(ctx, "sslmode=disable")
	require.NoError(t, err)
	admin, err := pgx.Connect(ctx, dsn)
	require.NoError(t, err)
	defer admin.Close(ctx)
	for _, s := range []string{"CREATE ROLE anvilkit_control_app LOGIN PASSWORD 'app'", "CREATE ROLE anvilkit_control_migrator LOGIN PASSWORD 'migrator'", "CREATE DATABASE anvilkit_control OWNER anvilkit_control_migrator"} {
		_, err = admin.Exec(ctx, s)
		require.NoError(t, err)
	}
	host, err := pgc.Host(ctx)
	require.NoError(t, err)
	port, err := pgc.MappedPort(ctx, "5432/tcp")
	require.NoError(t, err)
	url := func(role string) string {
		return fmt.Sprintf("postgres://anvilkit_control_%s:%s@%s:%s/anvilkit_control?sslmode=disable", role, role, host, port.Port())
	}
	out, err := migration(url("migrator")).CombinedOutput()
	require.NoError(t, err, string(out))
	t.Setenv("ANVILKIT_DEV_CONTROL_DSN", url("app"))
	t.Setenv("ANVILKIT_DEV_CONTROL_MIGRATOR_DSN", url("migrator"))
	isolatedTemporalAndObjects(t)
}

// isolatedTemporalAndObjects starts the disposable Temporal frontend and the
// versioned artifact store a private Control uses and points the harness at
// them.
func isolatedTemporalAndObjects(t *testing.T) {
	t.Helper()
	ctx := context.Background()
	temporal, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{ContainerRequest: testcontainers.ContainerRequest{Image: "temporalio/admin-tools:1.31.2", Entrypoint: []string{"temporal"}, Cmd: []string{"server", "start-dev", "--ip", "0.0.0.0", "--port", "7233", "--namespace", "anvilkit"}, ExposedPorts: []string{"7233/tcp"}, WaitingFor: wait.ForListeningPort("7233/tcp").WithStartupTimeout(2 * time.Minute)}, Started: true})
	require.NoError(t, err)
	testcontainers.CleanupContainer(t, temporal)
	address, err := temporal.Endpoint(ctx, "")
	require.NoError(t, err)
	t.Setenv("ANVILKIT_DEV_TEMPORAL_ADDRESS", address)
	objectStore, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{ContainerRequest: testcontainers.ContainerRequest{Image: "minio/minio:RELEASE.2025-09-07T16-13-09Z", Cmd: []string{"server", "/data"}, Env: map[string]string{"MINIO_ROOT_USER": "test-owner", "MINIO_ROOT_PASSWORD": "disposable-only"}, ExposedPorts: []string{"9000/tcp"}, WaitingFor: wait.ForHTTP("/minio/health/ready").WithPort("9000/tcp")}, Started: true})
	require.NoError(t, err)
	testcontainers.CleanupContainer(t, objectStore)
	code, _, err := objectStore.Exec(ctx, []string{"sh", "-c", "mc alias set isolated http://127.0.0.1:9000 test-owner disposable-only >/dev/null && mc mb isolated/artifacts >/dev/null && mc version enable isolated/artifacts >/dev/null"})
	require.NoError(t, err)
	require.Zero(t, code)
	endpoint, err := objectStore.Endpoint(ctx, "http")
	require.NoError(t, err)
	t.Setenv("ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT", endpoint)
	t.Setenv("ANVILKIT_CONTROL_ARTIFACTS_S3_BUCKET", "artifacts")
	t.Setenv("ANVILKIT_CONTROL_ARTIFACTS_S3_ACCESS_KEY_ID", "test-owner")
	t.Setenv("ANVILKIT_CONTROL_ARTIFACTS_S3_SECRET_ACCESS_KEY", "disposable-only")

}
