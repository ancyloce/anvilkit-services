//go:build integration

package integration

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/nats-io/nats.go"
	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"
	"github.com/testcontainers/testcontainers-go/wait"
)

// Every invocation owns its database server, queue and JetStream. No environment
// DSN is accepted; a namespace or queue prefix alone is not dependency isolation.
// P0.6: all three serve TLS from the run's CA and admit only authenticated
// clients (laneDataPlane).
func laneDependencies(t *testing.T, root string) (map[string]string, func(), *laneDataPlane) {
	t.Helper()
	ctx := context.Background()
	owner := uuid.NewString()
	env := map[string]string{}
	dp := newLaneDataPlane(t, ctx)
	cleanup := func(c testcontainers.Container) {
		t.Cleanup(func() {
			inspect, err := c.Inspect(ctx)
			if err != nil {
				t.Errorf("inspect owned dependency: %v", err)
				return
			}
			if inspect.Config.Labels["anvilkit.test.owner"] != owner {
				t.Error("refusing unowned dependency cleanup")
				return
			}
			require.NoError(t, c.Terminate(ctx))
		})
	}
	// The server key re-installed for the postgres user (it refuses a key
	// others can read), TLS on and pg_hba refusing every non-TLS connection.
	pgc, err := postgres.Run(ctx, "postgres:17-alpine", postgres.WithDatabase("postgres"), postgres.WithUsername("postgres"), postgres.WithPassword("disposable"), postgres.BasicWaitStrategies(), testcontainers.WithLabels(map[string]string{"anvilkit.test.owner": owner}),
		testcontainers.WithFiles(dp.files()...),
		testcontainers.WithEntrypoint("sh", "-c", `install -o postgres -g postgres -m 0600 /tls/tls.key /var/lib/postgresql/tls.key && install -o postgres -g postgres -m 0644 /tls/tls.crt /var/lib/postgresql/tls.crt && install -o postgres -g postgres -m 0644 /tls/pg_hba.conf /var/lib/postgresql/pg_hba.conf && exec docker-entrypoint.sh "$@" -c ssl=on -c ssl_cert_file=/var/lib/postgresql/tls.crt -c ssl_key_file=/var/lib/postgresql/tls.key -c hba_file=/var/lib/postgresql/pg_hba.conf`, "sh"))
	require.NoError(t, err)
	cleanup(pgc)
	dsn, err := pgc.ConnectionString(ctx, dp.dsnTLS())
	require.NoError(t, err)
	admin, err := pgx.Connect(ctx, dsn)
	require.NoError(t, err)
	defer admin.Close(ctx)
	host, err := pgc.Host(ctx)
	require.NoError(t, err)
	port, err := pgc.MappedPort(ctx, "5432/tcp")
	require.NoError(t, err)
	for _, domain := range []string{"knowledge", "mcp"} {
		roles := []string{"app", "migrator", "relay"}
		if domain == "knowledge" {
			// P17: the PostgresStore vendor migration identity and the runtime Store role.
			// P2.7: the cleanup identity the Knowledge migrations grant to.
			roles = append(roles, "forwarder", "store_migrator", "store", "cleanup")
		}
		for _, role := range roles {
			_, err = admin.Exec(ctx, fmt.Sprintf("CREATE ROLE anvilkit_%s_%s LOGIN PASSWORD 'disposable'", domain, role))
			require.NoError(t, err)
			suffix := "_" + strings.ToUpper(role)
			if role == "app" {
				suffix = ""
			}
			env["ANVILKIT_DEV_"+strings.ToUpper(domain)+suffix+"_DSN"] = fmt.Sprintf("postgres://anvilkit_%s_%s:disposable@%s:%s/anvilkit_%s?%s", domain, role, host, port.Port(), domain, dp.dsnTLS())
		}
		_, err = admin.Exec(ctx, fmt.Sprintf("CREATE DATABASE anvilkit_%s OWNER anvilkit_%s_migrator", domain, domain))
		require.NoError(t, err)
		args := []string{"run", "./cmd/anvilkit-migration", "-domain", domain}
		if domain == "mcp" {
			// P2.5: the lane's local-check kind is the DEVELOPMENT_ONLY fixture
			// set of MCP (sql/mcp-development), applied under its own version table.
			args = append(args, "-development")
		}
		cmd := exec.Command("go", args...)
		cmd.Dir = filepath.Join(root, "jobs/migration")
		cmd.Env = append(os.Environ(), "ANVILKIT_MIGRATION_DSN="+env["ANVILKIT_DEV_"+strings.ToUpper(domain)+"_MIGRATOR_DSN"])
		output, err := cmd.CombinedOutput()
		require.NoError(t, err, string(output))
		if domain == "knowledge" {
			// The memory Store's vendor schema, migrated by the Store's own
			// migrations under its identity (Knowledge's store-migrate entry).
			cmd := exec.Command("node", "dist/storemigrate.js")
			cmd.Dir = filepath.Join(root, "services/agent/knowledge")
			cmd.Env = append(os.Environ(), "ANVILKIT_KNOWLEDGE_STORE_MIGRATION_URL="+env["ANVILKIT_DEV_KNOWLEDGE_STORE_MIGRATOR_DSN"])
			output, err := cmd.CombinedOutput()
			require.NoError(t, err, string(output))
		}
	}
	start := func(image, port string, command []string, ready string) testcontainers.Container {
		c, err := testcontainers.GenericContainer(ctx, testcontainers.GenericContainerRequest{ContainerRequest: testcontainers.ContainerRequest{Image: image, ExposedPorts: []string{port}, Cmd: command, Files: dp.files(), Labels: map[string]string{"anvilkit.test.owner": owner}, WaitingFor: wait.ForLog(ready).WithStartupTimeout(time.Minute)}, Started: true})
		require.NoError(t, err)
		cleanup(c)
		return c
	}
	// Valkey: TLS on the only port, the default user off, one ACL user per role.
	queue := start("valkey/valkey:9.0.2-alpine", "6379/tcp", []string{"valkey-server", "--port", "0", "--tls-port", "6379", "--tls-cert-file", "/tls/tls.crt", "--tls-key-file", "/tls/tls.key",
		"--tls-ca-cert-file", "/tls/ca.crt", "--tls-auth-clients", "no", "--aclfile", "/tls/users.acl"}, "Ready to accept connections")
	endpoint, err := queue.Endpoint(ctx, "")
	require.NoError(t, err)
	dp.queue, dp.queueEndpoint = queue, endpoint
	env["ANVILKIT_DEV_QUEUE_CA_FILE"] = dp.ca
	// NATS: TLS only, NKey users with per-role subject permissions.
	broker := start("nats:2.12.4-alpine", "4222/tcp", []string{"-c", "/tls/nats.conf"}, "Server is ready")
	endpoint, err = broker.Endpoint(ctx, "")
	require.NoError(t, err)
	dp.natsURL = "tls://" + endpoint
	env["ANVILKIT_DEV_NATS_URL"] = dp.natsURL
	nc := dp.natsAdmin(t)
	js, err := nc.JetStream()
	require.NoError(t, err)
	for _, domain := range []string{"knowledge", "mcp"} {
		_, err = js.AddStream(&nats.StreamConfig{Name: "ANVILKIT_" + strings.ToUpper(domain), Subjects: []string{"anvilkit." + domain + ".>"}, Storage: nats.FileStorage, Duplicates: 2 * time.Minute})
		require.NoError(t, err)
	}
	nc.Close()
	for key, value := range env {
		t.Setenv(key, value)
	}
	reset := func() {
		inspect, err := queue.Inspect(ctx)
		require.NoError(t, err)
		require.Equal(t, owner, inspect.Config.Labels["anvilkit.test.owner"], "refusing unowned queue reset")
		code, _, err := queue.Exec(ctx, []string{"valkey-cli", "--tls", "--cacert", "/tls/ca.crt", "--sni", "localhost", "-p", "6379", "--user", "admin", "--pass", dp.queueUsers["admin"], "--no-auth-warning", "FLUSHDB"})
		require.NoError(t, err)
		require.Zero(t, code)
	}
	return env, reset, dp
}
