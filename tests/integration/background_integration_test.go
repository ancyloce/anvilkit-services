//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"

	knowledgev1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/knowledge/v1"
	mcpv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/mcp/v1"
)

// The P14 background lane on the development foundation (real PostgreSQL,
// NATS JetStream and the queue Valkey of deploy/dev): the Knowledge owner
// (Node) and the MCP owner (Go) commit requests with their outbox events,
// the Knowledge forwarder sidecar and MCP's forwarder module publish them to
// JetStream, one owner queue relay per owner turns them into BullMQ entries,
// the Background Worker claims, computes the local-check fixture and
// submits, and the owners accept. Then the failures: a worker killed
// mid-lease (expiry, reschedule, reconstruction, one accepted result), a
// lost queue projection (owned disposable Valkey) rebuilt from the durable requests, a relay
// killed before its consumer catches up, a forwarder killed and restarted
// (offsets resume), cancellation and revocation fencing a running handler,
// SIGTERM draining the worker, and the owners' readiness withdrawn on stop.

type laneProcess struct {
	t         *testing.T
	name      string
	cmd       *exec.Cmd
	log       *os.File
	healthURL string
}

type lane struct {
	t                                                                *testing.T
	root, dir, mcpBin, forwarderBin                                  string
	knowledgeAddr, mcpAddr, workerHealth, relayKHealth, relayMHealth string
	knowledgeHealth, mcpHealth, forwarderHealth                      string
	knowledgeCfg, mcpCfg, workerCfg                                  string
	resetQueue                                                       func()
	procs                                                            map[string]*laneProcess
	knowledge                                                        knowledgev1.BackgroundTaskServiceClient
	mcp                                                              mcpv1.BackgroundTaskServiceClient
	knowledgeEnv                                                     []string
	mcpEnv                                                           []string
	// P0.6: the run's TLS data plane and the Valkey password each ACL user
	// currently connects with (the rotation scenario changes the worker's).
	dataPlane     *laneDataPlane
	queuePassword map[string]string
}

// laneOptions adjust the lane for a scenario: extra replacements in
// Knowledge's tuned configuration and extra Knowledge environment (P15's
// object store, launcher and Inference placements).
type laneOptions struct {
	knowledgeReplacements [][2]string
	knowledgeEnv          []string
	// P18: MCP's tuned configuration and placements (Control, ContextForge,
	// role bindings).
	mcpReplacements [][2]string
	mcpEnv          []string
}

func laneEnv(t *testing.T, names ...string) map[string]string {
	t.Helper()
	out := map[string]string{}
	for _, n := range names {
		v := os.Getenv(n)
		if v == "" {
			skipMissing(t, n, "not set (source .local/dev/env.sh of the development foundation)")
		}
		out[n] = v
	}
	return out
}

func setupLane(t *testing.T) *lane {
	t.Helper()
	return setupLaneWith(t, laneOptions{})
}

func setupLaneWith(t *testing.T, opts laneOptions) *lane {
	t.Helper()
	root := repoRoot(t)
	env, resetQueue, dp := laneDependencies(t, root)
	for _, dist := range []string{"services/agent/knowledge/dist/main.js", "services/agent/knowledge/dist/localcheck.js", "services/agent/background-worker/dist/main.js"} {
		if _, err := os.Stat(filepath.Join(root, dist)); err != nil {
			skipMissing(t, "build:"+filepath.Dir(dist), "%s missing (pnpm run build in the package)", dist)
		}
	}
	dir := logDir(t)
	l := &lane{t: t, root: root, dir: dir, procs: map[string]*laneProcess{}, resetQueue: resetQueue, knowledgeEnv: opts.knowledgeEnv, mcpEnv: opts.mcpEnv,
		dataPlane: dp, queuePassword: map[string]string{}}
	for user, password := range dp.queueUsers {
		l.queuePassword[user] = password
	}
	l.mcpBin = filepath.Join(dir, "anvilkit-agent-mcp")
	l.forwarderBin = filepath.Join(dir, "anvilkit-knowledge-forwarder")
	for out, spec := range map[string][2]string{
		l.mcpBin:       {filepath.Join(root, "services", "agent", "mcp"), "./cmd/anvilkit-agent-mcp"},
		l.forwarderBin: {filepath.Join(root, "services", "agent", "knowledge", "forwarder"), "./cmd/anvilkit-knowledge-forwarder"},
	} {
		args := []string{"build", "-o", out, spec[1]}
		if out == l.mcpBin {
			// The DEVELOPMENT_ONLY local-check entry point (P2.5: build-tagged).
			args = []string{"build", "-tags", "localcheck", "-o", out, spec[1]}
		}
		cmd := exec.Command("go", args...)
		cmd.Dir = spec[0]
		raw, err := cmd.CombinedOutput()
		require.NoError(t, err, string(raw))
	}
	// Tuned bounds for the scenarios (short leases, retries and sweeps).
	l.knowledgeCfg = l.tunedConfig(filepath.Join(root, "services", "agent", "knowledge", "config.yaml"), "knowledge.yaml", append([][2]string{
		{"  retry_delay: 5s", "  retry_delay: 1s"}, {"  sweep_interval: 2s", "  sweep_interval: 500ms"}, {"  shutdown_timeout: 20s", "  shutdown_timeout: 5s"},
	}, opts.knowledgeReplacements...))
	l.mcpCfg = l.tunedConfig(filepath.Join(root, "services", "agent", "mcp", "config.yaml"), "mcp.yaml", append([][2]string{
		{"  retry_delay: 5s", "  retry_delay: 1s"}, {"  sweep_interval: 2s", "  sweep_interval: 500ms"}, {"  shutdown_timeout: 20s", "  shutdown_timeout: 5s"},
	}, opts.mcpReplacements...))
	l.workerCfg = l.tunedConfig(filepath.Join(root, "services", "agent", "background-worker", "config.yaml"), "worker.yaml", [][2]string{
		{"  lease_seconds: 60", "  lease_seconds: 5"}, {"  heartbeat_interval: 15s", "  heartbeat_interval: 1s"}, {"  lock_duration: 60s", "  lock_duration: 5s"},
		{"  stalled_interval: 30s", "  stalled_interval: 2s"}, {"  max_stalled_count: 1", "  max_stalled_count: 3"}, {"  shutdown_timeout: 30s", "  shutdown_timeout: 10s"},
		{"  reconcile_interval: 10s", "  reconcile_interval: 2s"}, {"  reconcile_age: 5s", "  reconcile_age: 1s"}, {"  ack_wait: 30s", "  ack_wait: 10s"},
	})
	l.knowledgeAddr = "127.0.0.1:" + freePort(t)
	l.mcpAddr = "127.0.0.1:" + freePort(t)
	l.knowledgeHealth = "127.0.0.1:" + freePort(t)
	l.mcpHealth = "127.0.0.1:" + freePort(t)
	l.forwarderHealth = "127.0.0.1:" + freePort(t)
	l.workerHealth = "127.0.0.1:" + freePort(t)
	l.relayKHealth = "127.0.0.1:" + freePort(t)
	l.relayMHealth = "127.0.0.1:" + freePort(t)

	// Authorization fixtures belong exclusively to this test run.
	l.sql(env["ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN"], `INSERT INTO sources (source_id, tenant_id, kind, locator, command_id, request_digest) VALUES ('src_int', 'tenant_a', 'document', 'file://integration', 'cmd_src_int', 'sha256:0000000000000000000000000000000000000000000000000000000000000000') ON CONFLICT (source_id) DO UPDATE SET deleted = false`)
	l.sql(env["ANVILKIT_DEV_MCP_MIGRATOR_DSN"], `INSERT INTO servers (server_id, tenant_id, canonical_resource, transport) VALUES ('srv_int', 'tenant_a', 'https://mcp.example/int', 'streamable-http') ON CONFLICT DO NOTHING`)
	l.sql(env["ANVILKIT_DEV_MCP_MIGRATOR_DSN"], `INSERT INTO descriptors (server_id, revision, protocol_version, provenance, descriptor_digest, tools, state, command_id, request_digest, data_class, revision_evidence, discovered_by, reviewer, review_id) VALUES ('srv_int', 1, '2025-06-18', 'test', 'sha256:0000000000000000000000000000000000000000000000000000000000000001', '[]', 'approved', 'cmd_int', 'sha256:0000000000000000000000000000000000000000000000000000000000000002', 'internal', 'fixture', 'fixture-discoverer', 'fixture-reviewer', 'rev_fixture') ON CONFLICT DO NOTHING`)
	l.sql(env["ANVILKIT_DEV_MCP_MIGRATOR_DSN"], `INSERT INTO grants (grant_id, tenant_id, subject_type, subject_id, server_id, descriptor_revision, descriptor_digest, methods, purpose, cost_cap_currency, cost_cap_amount, state, expires_at, command_id, request_digest, canonical_resource, transport, protocol_version, audience, data_class, policy_digest, registration_command_id, control_receipt_id) VALUES ('g_int', 'tenant_a', 'actor', 'user_a', 'srv_int', 1, 'sha256:0000000000000000000000000000000000000000000000000000000000000001', '{tools/call}', 'test', 'USD', 0, 'active', now() + interval '1 hour', 'cmd_grant_int', 'sha256:0000000000000000000000000000000000000000000000000000000000000003', 'https://mcp.example/int', 'streamable-http', '2025-06-18', 'https://mcp.example/int', 'internal', 'sha256:0000000000000000000000000000000000000000000000000000000000000004', 'reg_int', 'rcpt_fixture') ON CONFLICT (grant_id) DO UPDATE SET state = 'active', control_receipt_id = 'rcpt_fixture', revocation_command_id = NULL, control_state = 'none', revoked_at = NULL`)
	l.flushQueue()

	l.startMCP()
	l.startKnowledge()
	l.startForwarder()
	l.startRelay("knowledge")
	l.startRelay("mcp")
	l.startWorker()
	t.Cleanup(func() {
		for _, p := range l.procs {
			p.kill()
		}
	})
	kc := dialKnowledgeAs(t, l.knowledgeAddr, principalWorker)
	mc := dialMCPAs(t, l.mcpAddr, principalWorker)
	l.knowledge = knowledgev1.NewBackgroundTaskServiceClient(kc)
	l.mcp = mcpv1.NewBackgroundTaskServiceClient(mc)
	return l
}

func (l *lane) tunedConfig(src, name string, replacements [][2]string) string {
	raw, err := os.ReadFile(src)
	require.NoError(l.t, err)
	text := string(raw)
	for _, r := range replacements {
		require.Contains(l.t, text, r[0], name)
		text = strings.Replace(text, r[0], r[1], 1)
	}
	path := filepath.Join(l.dir, name)
	require.NoError(l.t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

func (l *lane) sql(dsn, stmt string, args ...any) {
	l.t.Helper()
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn)
	require.NoError(l.t, err)
	defer conn.Close(ctx)
	_, err = conn.Exec(ctx, stmt, args...)
	require.NoError(l.t, err)
}

func (l *lane) query(dsn, stmt string, args ...any) []map[string]any {
	l.t.Helper()
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn)
	require.NoError(l.t, err)
	defer conn.Close(ctx)
	rows, err := conn.Query(ctx, stmt, args...)
	require.NoError(l.t, err)
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		values, err := rows.Values()
		require.NoError(l.t, err)
		m := map[string]any{}
		for i, f := range rows.FieldDescriptions() {
			m[f.Name] = values[i]
		}
		out = append(out, m)
	}
	return out
}

func (l *lane) flushQueue() {
	l.t.Helper()
	l.resetQueue()
}

func (l *lane) spawn(name, healthURL string, env []string, dirOf string, args ...string) *laneProcess {
	l.t.Helper()
	p := &laneProcess{t: l.t, name: name, healthURL: healthURL}
	logf, err := os.OpenFile(filepath.Join(l.dir, name+".log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	require.NoError(l.t, err)
	cmd := exec.Command(args[0], args[1:]...)
	cmd.Dir = dirOf
	cmd.Env = append([]string{"PATH=" + os.Getenv("PATH"), "HOME=" + os.Getenv("HOME")}, env...)
	cmd.Stdout, cmd.Stderr = logf, logf
	require.NoError(l.t, cmd.Start(), name)
	p.cmd, p.log = cmd, logf
	l.procs[name] = p
	p.waitReady()
	return p
}

func (p *laneProcess) waitReady() {
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		if resp, err := http.Get("http://" + p.healthURL + "/readyz"); err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 {
				return
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	raw, _ := os.ReadFile(p.log.Name())
	p.t.Fatalf("%s did not become ready:\n%s", p.name, raw)
}

func (p *laneProcess) kill() {
	if p.cmd != nil && p.cmd.Process != nil {
		_ = p.cmd.Process.Kill()
		_ = p.cmd.Wait()
		p.cmd = nil
	}
}

func (p *laneProcess) term(timeout time.Duration) time.Duration {
	started := time.Now()
	if p.cmd != nil && p.cmd.Process != nil {
		_ = p.cmd.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { _ = p.cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(timeout):
			_ = p.cmd.Process.Kill()
			p.t.Errorf("%s did not stop within %s", p.name, timeout)
		}
		p.cmd = nil
	}
	return time.Since(started)
}

// mcpProcessEnv is the environment of the MCP process and of its operator
// subcommands (the same configuration, placements and workload identity).
func (l *lane) mcpProcessEnv() []string {
	return append([]string{
		"ANVILKIT_MCP_CONFIG=" + l.mcpCfg, "ANVILKIT_MCP_DATABASE_URL=" + os.Getenv("ANVILKIT_DEV_MCP_DSN"),
		"ANVILKIT_MCP_LISTEN=" + l.mcpAddr, "ANVILKIT_MCP_HEALTH_LISTEN=" + l.mcpHealth,
	}, append(append(identityEnv(l.t, "ANVILKIT_MCP_", principalMCP), l.dataPlane.natsEnv("ANVILKIT_MCP_", "mcp-forwarder")...), l.mcpEnv...)...)
}

func (l *lane) startMCP() {
	l.spawn("mcp", l.mcpHealth, l.mcpProcessEnv(), l.dir, l.mcpBin)
}

// mcpCommand runs an operator subcommand of the MCP binary (P1.2:
// `reconcile <callId>`) under the process's environment and returns its stdout.
func (l *lane) mcpCommand(args ...string) ([]byte, error) {
	cmd := exec.Command(l.mcpBin, args...)
	cmd.Dir = l.dir
	cmd.Env = append([]string{"PATH=" + os.Getenv("PATH"), "HOME=" + os.Getenv("HOME")}, l.mcpProcessEnv()...)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return append(out, []byte(stderr.String())...), err
	}
	return out, nil
}

func (l *lane) startKnowledge() {
	l.spawn("knowledge", l.knowledgeHealth, append([]string{
		"ANVILKIT_KNOWLEDGE_CONFIG=" + l.knowledgeCfg, "ANVILKIT_KNOWLEDGE_DATABASE_URL=" + os.Getenv("ANVILKIT_DEV_KNOWLEDGE_DSN"),
		"ANVILKIT_KNOWLEDGE_LISTEN=" + l.knowledgeAddr, "ANVILKIT_KNOWLEDGE_HEALTH_LISTEN=" + l.knowledgeHealth,
		// P17: the lane's own runtime Store role (its vendor schema is migrated by laneDependencies).
		"ANVILKIT_KNOWLEDGE_STORE_DATABASE_URL=" + os.Getenv("ANVILKIT_DEV_KNOWLEDGE_STORE_DSN"),
	}, append(identityEnv(l.t, "ANVILKIT_KNOWLEDGE_", principalKnowledge), l.knowledgeEnv...)...), filepath.Join(l.root, "services", "agent", "knowledge"), "node", "dist/main.js")
}

func (l *lane) startForwarder() {
	// P0.6: no DEVELOPMENT_ONLY guard: the forwarder's database and JetStream
	// are both TLS with verification, JetStream with its own NKey user.
	l.spawn("forwarder", l.forwarderHealth, append([]string{
		"ANVILKIT_FORWARDER_DATABASE_URL=" + os.Getenv("ANVILKIT_DEV_KNOWLEDGE_FORWARDER_DSN"),
		"ANVILKIT_FORWARDER_HEALTH_LISTEN=" + l.forwarderHealth, "ANVILKIT_FORWARDER_POLL_INTERVAL=200ms", "ANVILKIT_FORWARDER_CLOSE_TIMEOUT=5s",
	}, l.dataPlane.natsEnv("ANVILKIT_FORWARDER_", "knowledge-forwarder")...), l.dir, l.forwarderBin)
}

// workerEnv is the Background Worker's environment under one Valkey ACL user
// (rediss:// with the run's CA); the relays add their NATS user.
func (l *lane) workerEnv(queueUser string) []string {
	return []string{
		"ANVILKIT_BACKGROUND_WORKER_CONFIG=" + l.workerCfg,
		"ANVILKIT_BACKGROUND_WORKER_QUEUE_URL=" + l.dataPlane.queueURL(queueUser, l.queuePassword[queueUser]), "ANVILKIT_BACKGROUND_WORKER_QUEUE_TLS_CA_FILE=" + l.dataPlane.ca,
		"ANVILKIT_BACKGROUND_WORKER_KNOWLEDGE_ADDRESS=" + l.knowledgeAddr, "ANVILKIT_BACKGROUND_WORKER_MCP_ADDRESS=" + l.mcpAddr, "ANVILKIT_BACKGROUND_WORKER_CONTRACTS_DIR=" + filepath.Join(l.root, "contracts"),
		"ANVILKIT_BACKGROUND_WORKER_IDENTITY_MODE=mtls",
	}
}

// relayIdentity is the owner's own workload identity: the relay runs inside
// the owner's Pod and presents the Pod's certificate (last wins in the env).
func (l *lane) relayIdentity(owner string) []string {
	principal, server := principalKnowledge, "anvilkit-agent-knowledge"
	if owner == "mcp" {
		principal, server = principalMCP, "anvilkit-agent-mcp"
	}
	return append(identityEnv(l.t, "ANVILKIT_BACKGROUND_WORKER_", principal), "ANVILKIT_BACKGROUND_WORKER_OWNER_SERVER_NAME="+server)
}

func (l *lane) startRelay(owner string) {
	health := l.relayKHealth
	dsn := os.Getenv("ANVILKIT_DEV_KNOWLEDGE_RELAY_DSN")
	if owner == "mcp" {
		health, dsn = l.relayMHealth, os.Getenv("ANVILKIT_DEV_MCP_RELAY_DSN")
	}
	env := append(append(l.workerEnv("relay-"+owner), l.dataPlane.natsEnv("ANVILKIT_BACKGROUND_WORKER_", owner+"-relay")...),
		"ANVILKIT_BACKGROUND_WORKER_HEALTH_LISTEN="+health, "ANVILKIT_BACKGROUND_WORKER_RELAY_OWNER="+owner, "ANVILKIT_BACKGROUND_WORKER_RELAY_DATABASE_URL="+dsn)
	l.spawn("relay-"+owner, health, append(env, l.relayIdentity(owner)...),
		filepath.Join(l.root, "services", "agent", "background-worker"), "node", "dist/main.js", "relay")
}

func (l *lane) startWorker() {
	l.spawn("worker", l.workerHealth, append(append(l.workerEnv("worker"), "ANVILKIT_BACKGROUND_WORKER_HEALTH_LISTEN="+l.workerHealth), identityEnv(l.t, "ANVILKIT_BACKGROUND_WORKER_", principalWorker)...),
		filepath.Join(l.root, "services", "agent", "background-worker"), "node", "dist/main.js", "worker")
}

type created struct {
	TaskID               string `json:"taskId"`
	Generation           uint64 `json:"generation"`
	InputDigest          string `json:"inputDigest"`
	ExpectedResultDigest string `json:"expectedResultDigest"`
}

// requestKnowledge creates a local-check request through the Knowledge
// owner's own entry point (the Node producer).
func (l *lane) requestKnowledge(args ...string) created {
	l.t.Helper()
	cmd := exec.Command("node", append([]string{"dist/localcheck.js", "request", "--source", "src_int"}, args...)...)
	cmd.Dir = filepath.Join(l.root, "services", "agent", "knowledge")
	cmd.Env = append(os.Environ(), "ANVILKIT_KNOWLEDGE_CONFIG="+l.knowledgeCfg, "ANVILKIT_KNOWLEDGE_DATABASE_URL="+os.Getenv("ANVILKIT_DEV_KNOWLEDGE_DSN"))
	out, err := cmd.Output()
	if ee, ok := err.(*exec.ExitError); ok {
		l.t.Fatalf("knowledge localcheck: %v: %s", err, ee.Stderr)
	}
	require.NoError(l.t, err)
	var c created
	require.NoError(l.t, json.Unmarshal(out, &c), string(out))
	return c
}

func (l *lane) cancelKnowledge(taskID string) {
	l.t.Helper()
	cmd := exec.Command("node", "dist/localcheck.js", "cancel", "--task-id", taskID)
	cmd.Dir = filepath.Join(l.root, "services", "agent", "knowledge")
	cmd.Env = append(os.Environ(), "ANVILKIT_KNOWLEDGE_CONFIG="+l.knowledgeCfg, "ANVILKIT_KNOWLEDGE_DATABASE_URL="+os.Getenv("ANVILKIT_DEV_KNOWLEDGE_DSN"))
	raw, err := cmd.CombinedOutput()
	require.NoError(l.t, err, string(raw))
}

// requestMCP creates a local-check request through the MCP owner's own
// entry point (the Go producer).
func (l *lane) requestMCP(args ...string) created {
	l.t.Helper()
	cmd := exec.Command(l.mcpBin, append([]string{"local-check", "request", "-grant", "g_int"}, args...)...)
	cmd.Env = append(append(os.Environ(), "ANVILKIT_MCP_CONFIG="+l.mcpCfg, "ANVILKIT_MCP_DATABASE_URL="+os.Getenv("ANVILKIT_DEV_MCP_DSN")), l.dataPlane.natsEnv("ANVILKIT_MCP_", "mcp-forwarder")...)
	out, err := cmd.Output()
	if ee, ok := err.(*exec.ExitError); ok {
		l.t.Fatalf("mcp local-check: %v: %s", err, ee.Stderr)
	}
	require.NoError(l.t, err)
	var c created
	require.NoError(l.t, json.Unmarshal(out, &c), string(out))
	return c
}

func (l *lane) knowledgeState(taskID string) (state string, attempts string, worker string) {
	resp, err := l.knowledge.GetTask(context.Background(), &knowledgev1.GetTaskRequest{TaskId: taskID})
	require.NoError(l.t, err)
	return strings.TrimPrefix(resp.GetTask().GetState().String(), "TASK_STATE_"), resp.GetTask().GetAttemptCount(), resp.GetTask().GetWorkerId()
}

func (l *lane) mcpState(taskID string) string {
	resp, err := l.mcp.GetTask(context.Background(), &mcpv1.GetTaskRequest{TaskId: taskID})
	require.NoError(l.t, err)
	return strings.TrimPrefix(resp.GetTask().GetState().String(), "TASK_STATE_")
}

func (l *lane) waitState(owner, taskID, want string, timeout time.Duration) {
	l.t.Helper()
	deadline := time.Now().Add(timeout)
	last := ""
	for time.Now().Before(deadline) {
		if owner == "knowledge" {
			last, _, _ = l.knowledgeState(taskID)
		} else {
			last = l.mcpState(taskID)
		}
		if last == want {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	l.t.Fatalf("%s task %s: state %s, want %s", owner, taskID, last, want)
}

func (l *lane) eventually(timeout time.Duration, what string, fn func() bool) {
	l.t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if fn() {
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	l.t.Fatalf("%s: not satisfied within %s", what, timeout)
}

func (l *lane) metric(health, name string) string {
	resp, err := http.Get("http://" + health + "/metrics")
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	for _, line := range strings.Split(string(body), "\n") {
		if strings.HasPrefix(line, name+" ") || strings.HasPrefix(line, name+"{") {
			return line
		}
	}
	return ""
}

func TestBackgroundLane(t *testing.T) {
	l := setupLane(t)
	kDSN := os.Getenv("ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN")
	mDSN := os.Getenv("ANVILKIT_DEV_MCP_MIGRATOR_DSN")

	t.Run("F06 recreated jobs exhaust business retries in one live worker with distinct identities", func(t *testing.T) {
		created := l.requestKnowledge("--bytes", "failure", "--fail")
		taskID := created.TaskID
		l.waitState("knowledge", taskID, "DEAD", 30*time.Second)
		rows := l.query(kDSN, "SELECT worker_id FROM task_attempts WHERE task_id=$1 ORDER BY ordinal", taskID)
		require.Len(t, rows, 3)
		identities := map[any]bool{}
		for _, row := range rows {
			require.False(t, identities[row["worker_id"]], "a recreated job reused its consumed worker identity")
			identities[row["worker_id"]] = true
		}
	})

	t.Run("owner request and outbox to NATS, inbox, relay, BullMQ, Worker and acceptance for both owners", func(t *testing.T) {
		k := l.requestKnowledge("--bytes", "knowledge-lane")
		m := l.requestMCP("-bytes", "mcp-lane")
		l.waitState("knowledge", k.TaskID, "ACCEPTED", 60*time.Second)
		l.waitState("mcp", m.TaskID, "ACCEPTED", 60*time.Second)
		kr := l.query(kDSN, "SELECT result_digest, attempt_count FROM background_requests WHERE task_id = $1", k.TaskID)
		require.Equal(t, k.ExpectedResultDigest, kr[0]["result_digest"])
		mr := l.query(mDSN, "SELECT result_digest, attempt_count FROM background_requests WHERE task_id = $1", m.TaskID)
		require.Equal(t, m.ExpectedResultDigest, mr[0]["result_digest"])
		require.NotEmpty(t, l.query(kDSN, "SELECT 1 FROM inbox WHERE consumer = 'anvilkit-agent-knowledge-background-relay' AND outcome = 'enqueue'"))
		require.NotEmpty(t, l.query(mDSN, "SELECT 1 FROM inbox WHERE consumer = 'anvilkit-agent-mcp-background-relay' AND outcome = 'enqueue'"))
		// Both forwarders acknowledge every outbox row (the requested events;
		// background.completed left the catalog in P2.8).
		l.eventually(10*time.Second, "knowledge forwarder offsets", func() bool {
			return len(l.query(kDSN, `SELECT 1 FROM outbox_offsets WHERE consumer_group = 'anvilkit-agent-knowledge-forwarder' AND offset_acked >= (SELECT max("offset") FROM outbox)`)) > 0
		})
		l.eventually(10*time.Second, "mcp forwarder offsets", func() bool {
			return len(l.query(mDSN, `SELECT 1 FROM outbox_offsets WHERE consumer_group = 'anvilkit-agent-mcp-forwarder' AND offset_acked >= (SELECT max("offset") FROM outbox)`)) > 0
		})
	})

	t.Run("a worker killed mid-lease: expiry, reschedule, reconstruction, one accepted result", func(t *testing.T) {
		k := l.requestKnowledge("--bytes", "stall", "--hold-ms", "8000")
		l.waitState("knowledge", k.TaskID, "LEASED", 30*time.Second)
		_, _, first := l.knowledgeState(k.TaskID)
		l.procs["worker"].kill()
		// The lease (5s) expires without heartbeats; the sweeper reschedules; the
		// relay rebuilds the queue entry; a new worker takes attempt 2.
		l.waitState("knowledge", k.TaskID, "RETRY_SCHEDULED", 30*time.Second)
		l.startWorker()
		l.waitState("knowledge", k.TaskID, "ACCEPTED", 60*time.Second)
		_, attempts, second := l.knowledgeState(k.TaskID)
		require.Equal(t, "2", attempts)
		require.NotEqual(t, first, second)
		outcomes := l.query(kDSN, "SELECT outcome FROM task_attempts WHERE task_id = $1 ORDER BY ordinal", k.TaskID)
		require.Equal(t, "expired", outcomes[0]["outcome"])
		require.Equal(t, "accepted", outcomes[1]["outcome"])
		require.Len(t, l.query(kDSN, "SELECT 1 FROM task_attempts WHERE task_id = $1 AND outcome = 'accepted'", k.TaskID), 1)
	})

	t.Run("a lost queue write is reconstructed from the durable requests", func(t *testing.T) {
		l.procs["worker"].kill()
		k := l.requestKnowledge("--bytes", "lost-write")
		m := l.requestMCP("-bytes", "lost-write")
		time.Sleep(2 * time.Second)
		l.flushQueue()
		l.startWorker()
		l.waitState("knowledge", k.TaskID, "ACCEPTED", 60*time.Second)
		l.waitState("mcp", m.TaskID, "ACCEPTED", 60*time.Second)
		require.NotEqual(t, "", l.metric(l.relayKHealth, "anvilkit_background_relay_reconstructed_total"))
		require.NotContains(t, l.metric(l.relayKHealth, "anvilkit_background_relay_reconstructed_total"), " 0")
	})

	t.Run("a relay killed and restarted resumes its durable consumer; a forwarder killed and restarted resumes its offsets", func(t *testing.T) {
		l.procs["relay-knowledge"].kill()
		l.procs["forwarder"].kill()
		k := l.requestKnowledge("--bytes", "relay-down")
		time.Sleep(time.Second)
		state, _, _ := l.knowledgeState(k.TaskID)
		require.Equal(t, "PENDING", state)
		l.startForwarder()
		l.startRelay("knowledge")
		l.waitState("knowledge", k.TaskID, "ACCEPTED", 60*time.Second)
	})

	t.Run("cancellation and revocation fence a running handler", func(t *testing.T) {
		c := l.requestKnowledge("--bytes", "cancel", "--hold-ms", "6000")
		l.waitState("knowledge", c.TaskID, "LEASED", 30*time.Second)
		l.cancelKnowledge(c.TaskID)
		l.waitState("knowledge", c.TaskID, "CANCELED", 10*time.Second)
		r := l.requestKnowledge("--bytes", "revoke", "--hold-ms", "4000")
		l.waitState("knowledge", r.TaskID, "LEASED", 30*time.Second)
		l.sql(kDSN, "UPDATE sources SET deleted = true WHERE source_id = 'src_int'")
		l.waitState("knowledge", r.TaskID, "CANCELED", 30*time.Second)
		l.sql(kDSN, "UPDATE sources SET deleted = false WHERE source_id = 'src_int'")
		row := l.query(kDSN, "SELECT failure_code FROM background_requests WHERE task_id = $1", r.TaskID)
		require.Equal(t, "AUTHORIZATION_REVOKED", row[0]["failure_code"])
		time.Sleep(3 * time.Second)
		require.Empty(t, l.query(kDSN, "SELECT 1 FROM task_attempts WHERE task_id = $1 AND outcome = 'accepted'", r.TaskID))
		require.Empty(t, l.query(kDSN, "SELECT 1 FROM task_attempts WHERE task_id = $1 AND outcome = 'accepted'", c.TaskID))
	})

	t.Run("P0.6 the data plane admits only authenticated clients over TLS, each within its own scope", l.probeDataPlane)

	t.Run("P0.6 a queue ACL password rotates without restarting Valkey", l.rotateQueuePassword)

	t.Run("SIGTERM drains the worker and withdraws the owners' readiness before they stop", func(t *testing.T) {
		k := l.requestKnowledge("--bytes", "drain", "--hold-ms", "3000")
		l.waitState("knowledge", k.TaskID, "LEASED", 30*time.Second)
		took := l.procs["worker"].term(30 * time.Second)
		require.Greater(t, took, time.Second, "the in-flight handler finished before the exit")
		state, _, _ := l.knowledgeState(k.TaskID)
		require.Equal(t, "ACCEPTED", state)
		// The owners: readiness withdrawn, drain recorded, exit within the bound.
		for _, p := range []string{"knowledge", "mcp"} {
			health := l.knowledgeHealth
			if p == "mcp" {
				health = l.mcpHealth
			}
			took := l.procs[p].term(30 * time.Second)
			require.Less(t, took, 20*time.Second)
			_, err := http.Get("http://" + health + "/readyz")
			require.Error(t, err, "listener closed last")
		}
		for _, p := range []string{"forwarder", "relay-knowledge", "relay-mcp"} {
			require.Less(t, l.procs[p].term(30*time.Second), 20*time.Second)
		}
	})
}
