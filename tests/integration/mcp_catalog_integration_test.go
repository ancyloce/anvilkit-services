//go:build integration

package integration

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	mcpv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/mcp/v1"
)

// P18 on the development foundation: the private MCP catalog and the grant
// barrier across real processes — MCP (its own lane database, NATS and
// queue), one real Control process on the foundation's Control database, a disposable ContextForge v1.0.11 as the connection layer, and
// a fixture MCP server built with the official Go MCP SDK. Asserted:
// discovery through ContextForge binds the live descriptor (a listing that
// disagrees with the live server, including its negotiated protocol
// version, is refused); four-eyes review and role separation; a grant is
// executable only after Control's receipt, and every registration boundary
// (Control down, a registration applied whose answer was lost, MCP killed
// with a pending grant) stays non-executable and completes under the
// original command identity; a revocation denies new MCP admission at its
// commit, Control's fence denies new tool admission, and REVOKED waits for
// a sender holding a consumed permission — across a Control outage and an
// MCP crash; a changed server is a new revision that grants do not follow;
// disabling a revision revokes its grants.

const cfImage = "ghcr.io/ibm/mcp-context-forge:v1.0.11@sha256:e9639c030162d6cb2f9f188600dd69af8091ec62a7ce307d4a74f4d4f86873df"

func randomHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// ownContextForge starts a disposable ContextForge on host networking (the
// fixture listens on loopback) and mints an admin API token into a file.
// SSRF protection is off in this DEVELOPMENT_ONLY instance only because its
// handshake probe refuses loopback targets; MCP's own network policy still
// checks every target before it reaches ContextForge.
func ownContextForge(t *testing.T, dir string) (baseURL, tokenFile string) {
	t.Helper()
	port := freePort(t)
	secret := randomHex(32)
	password := "Fx-" + randomHex(8) + "-Pw!"
	name := "anvilkit-p18-cf-" + randomHex(4)
	out, err := exec.Command("docker", "run", "-d", "--name", name, "--network", "host",
		"-e", "HOST=127.0.0.1", "-e", "PORT="+port, "-e", "JWT_SECRET_KEY="+secret, "-e", "AUTH_ENCRYPTION_SECRET="+randomHex(32),
		"-e", "PLATFORM_ADMIN_EMAIL=admin@example.com", "-e", "PLATFORM_ADMIN_PASSWORD="+password, "-e", "DEFAULT_USER_PASSWORD="+password,
		"-e", "SSRF_PROTECTION_ENABLED=false", "-e", "MCP_CLIENT_CONNECT_MODE=auto", "-e", "MCPGATEWAY_UI_ENABLED=false",
		"-e", "MCPGATEWAY_ADMIN_API_ENABLED=false", "-e", "CACHE_TYPE=database", "-e", "GUNICORN_WORKERS=1", cfImage).CombinedOutput()
	if err != nil {
		skipMissing(t, "image:contextforge", "ContextForge %s did not start (%s)", cfImage, strings.TrimSpace(string(out)))
	}
	t.Cleanup(func() { _ = exec.Command("docker", "rm", "-f", name).Run() })
	baseURL = "http://127.0.0.1:" + port
	deadline := time.Now().Add(3 * time.Minute)
	for {
		resp, err := http.Get(baseURL + "/health")
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 {
				break
			}
		}
		if time.Now().After(deadline) {
			logs, _ := exec.Command("docker", "logs", "--tail", "40", name).CombinedOutput()
			t.Fatalf("ContextForge did not become healthy:\n%s", logs)
		}
		time.Sleep(2 * time.Second)
	}
	token, err := exec.Command("docker", "exec", name, "python", "-m", "mcpgateway.utils.create_jwt_token",
		"-u", "admin@example.com", "--admin", "--exp", "180", "--secret", secret).Output()
	require.NoError(t, err)
	lines := strings.Split(strings.TrimSpace(string(token)), "\n")
	tokenFile = filepath.Join(dir, "contextforge.token")
	require.NoError(t, os.WriteFile(tokenFile, []byte(lines[len(lines)-1]), 0o600))
	return baseURL, tokenFile
}

type fixtureServer struct {
	t     *testing.T
	bin   string
	addr  string
	cmd   *exec.Cmd
	extra []string // P19: the authorization server's flags
}

func (f *fixtureServer) start(variant int) {
	f.t.Helper()
	f.stop()
	f.cmd = exec.Command(f.bin, append([]string{"-listen", f.addr, "-variant", fmt.Sprint(variant)}, f.extra...)...)
	logf, err := os.Create(filepath.Join(filepath.Dir(f.bin), fmt.Sprintf("fixture-%d.log", variant)))
	require.NoError(f.t, err)
	f.cmd.Stdout, f.cmd.Stderr = logf, logf
	require.NoError(f.t, f.cmd.Start())
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if resp, err := http.Get("http://" + f.addr + "/healthz"); err == nil {
			resp.Body.Close()
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	f.t.Fatal("the MCP fixture did not start")
}

func (f *fixtureServer) stop() {
	if f.cmd != nil && f.cmd.Process != nil {
		_ = f.cmd.Process.Kill()
		_ = f.cmd.Wait()
		f.cmd = nil
	}
}

// serverIDOf is MCP's server identity for a tenant's canonical resource.
func serverIDOf(tenant, resource string) string {
	h := sha256.New()
	h.Write([]byte(tenant))
	h.Write([]byte{0})
	h.Write([]byte(resource))
	h.Write([]byte{0})
	return "srv-" + hex.EncodeToString(h.Sum(nil))[:32]
}

func TestMCPCatalog(t *testing.T) {
	laneEnv(t, "ANVILKIT_DEV_CONTROL_DSN", "ANVILKIT_DEV_TEMPORAL_ADDRESS")
	root := repoRoot(t)
	dir := t.TempDir()
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	// The upstream fixture and the connection layer.
	fixture := &fixtureServer{t: t, bin: filepath.Join(dir, "mcpfixture"), addr: "127.0.0.1:" + freePort(t)}
	build := exec.Command("go", "build", "-o", fixture.bin, "./cmd/mcpfixture")
	build.Dir = filepath.Join(root, "tests", "integration")
	out, err := build.CombinedOutput()
	require.NoError(t, err, string(out))
	fixture.start(1)
	t.Cleanup(fixture.stop)
	resource := "http://" + fixture.addr + "/mcp"
	serverID := serverIDOf("tenant_a", resource)
	cfURL, tokenFile := ownContextForge(t, dir)

	// Control (foundation database) with a price for the fixture's read method.
	base := dispatchControlConfig(t, root, dir)
	raw, err := os.ReadFile(base)
	require.NoError(t, err)
	controlCfg := filepath.Join(dir, "control.mcp.yaml")
	require.NoError(t, os.WriteFile(controlCfg, []byte(strings.Replace(string(raw), "route: srv/read", "route: "+serverID+"/search_issues", 1)), 0o600))
	e := setupProcesses(t, false, "", controlCfg, true)
	pools := e.db()
	for _, row := range []struct {
		level         string
		tenant, actor *string
		cap           int64
	}{{"platform", nil, nil, 1_000_000_000}, {"tenant", strPtr("tenant_a"), nil, 100_000_000}, {"actor", strPtr("tenant_a"), strPtr("user_a"), 10_000_000}} {
		_, err := pools.Exec(ctx, "INSERT INTO budget_pools (pool_id, level, tenant_id, actor_id, period_start, period_end, currency, cap_amount) VALUES ($1, $2, $3, $4, now() - interval '1 hour', now() + interval '1 hour', 'USD', $5)",
			"pool-"+run+"-"+row.level, row.level, row.tenant, row.actor, row.cap)
		require.NoError(t, err)
	}
	t.Cleanup(func() {
		_, _ = pools.Exec(context.Background(), "UPDATE budget_pools SET period_end = period_start + interval '1 second' WHERE pool_id LIKE $1", "pool-"+run+"-%")
	})

	// MCP in its own lane, with Control, ContextForge and the role bindings.
	roles := filepath.Join(dir, "roles.json")
	bindings, _ := json.Marshal([]map[string]string{
		{"tenantId": "tenant_a", "actorId": "carol", "role": "catalog_manager"},
		{"tenantId": "tenant_a", "actorId": "rita", "role": "catalog_reviewer"},
		{"tenantId": "tenant_a", "actorId": "gus", "role": "grant_manager"},
	})
	require.NoError(t, os.WriteFile(roles, bindings, 0o600))
	l := setupLaneWith(t, laneOptions{
		mcpReplacements: [][2]string{{"  private_hosts: []", `  private_hosts: ["127.0.0.1"]`}, {"  reconcile_interval: 2s", "  reconcile_interval: 300ms"}, {"  reconcile_age: 5s", "  reconcile_age: 1s"}},
		mcpEnv:          []string{"ANVILKIT_MCP_CONTROL_ADDRESS=" + e.controlAddr, "ANVILKIT_MCP_CONTEXTFORGE_URL=" + cfURL, "ANVILKIT_MCP_CONTEXTFORGE_TOKEN_FILE=" + tokenFile, "ANVILKIT_MCP_ROLES_FILE=" + roles},
	})
	mcpConn := dialMCPAs(t, l.mcpAddr, principalAPI)
	catalog, grants := mcpv1.NewCatalogServiceClient(mcpConn), mcpv1.NewGrantServiceClient(mcpConn)
	ctlMCP := e.dialControl(e.controlAddr, principalMCP)
	policy, dispatch := controlv1.NewGrantPolicyServiceClient(ctlMCP), controlv1.NewDispatchServiceClient(ctlMCP)
	ops, execs := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	mcpDSN := os.Getenv("ANVILKIT_DEV_MCP_MIGRATOR_DSN")

	seq := 0
	command := func(actor string) *mcpv1.CommandIdentity {
		seq++
		id := fmt.Sprintf("p18-%s-%d", run, seq)
		return &mcpv1.CommandIdentity{TenantId: "tenant_a", CommandId: id, ActorId: actor, RequestDigest: digestOf(id)}
	}
	scope := func(actor string) *mcpv1.Scope {
		return &mcpv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: actor}
	}
	tools := func() []*mcpv1.ToolSchema {
		return []*mcpv1.ToolSchema{
			{Method: "search_issues", UnitPrice: &mcpv1.Money{Currency: "USD", Amount: "1"}, Idempotent: true},
			{Method: "create_issue", SideEffecting: true, UnitPrice: &mcpv1.Money{Currency: "USD", Amount: "5"}},
		}
	}
	discover := func(protocol string, ts []*mcpv1.ToolSchema) (*mcpv1.DiscoverServerResponse, error) {
		return catalog.DiscoverServer(ctx, &mcpv1.DiscoverServerRequest{Command: command("carol"), Scope: scope("carol"), CanonicalResource: resource,
			Transport: "streamable-http", ProtocolVersion: protocol, Provenance: "fixture:anvilkit-issues", Tools: ts, DataClass: "internal"})
	}
	createGrant := func(actor string, d *mcpv1.Descriptor) (*mcpv1.CreateGrantResponse, error) {
		return grants.CreateGrant(ctx, &mcpv1.CreateGrantRequest{Command: command(actor), Scope: scope(actor), SubjectType: "project", SubjectId: "proj_a",
			ServerId: d.GetServerId(), DescriptorRevision: d.GetRevision(), DescriptorDigest: d.GetDescriptorDigest(), Methods: []string{"search_issues"},
			Purpose: "triage", CostCap: &mcpv1.Money{Currency: "USD", Amount: "1000"}, ResourceSelectors: []string{"open-issues"}})
	}
	grantState := func(id string) *mcpv1.GetRevocationProgressResponse {
		p, err := grants.GetRevocationProgress(ctx, &mcpv1.GetRevocationProgressRequest{Scope: scope("gus"), GrantId: id})
		require.NoError(t, err)
		return p
	}
	waitGrant := func(id string, want mcpv1.GrantState, what string) *mcpv1.GetRevocationProgressResponse {
		var last *mcpv1.GetRevocationProgressResponse
		l.eventually(2*time.Minute, what, func() bool {
			last = grantState(id)
			return last.GetGrant().GetState() == want
		})
		return last
	}
	opID, attemptID := e.fundedOperation(ctx, t, ops, execs, run, "mcp", 100_000)
	admit := func(grantID, call string) (*controlv1.AdmitToolResponse, error) {
		return dispatch.AdmitTool(ctx, &controlv1.AdmitToolRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "admit-" + run + "-" + call, ActorId: "mcp", RequestDigest: digestOf(call)},
			Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
			CallId:  run + "-" + call, Owner: "anvilkit-agent-mcp", GrantId: grantID, GrantRevision: "1", ServerId: serverID, Method: "search_issues",
			ArgumentDigest: digestOf("args-" + call), MaxExposure: &controlv1.Money{Currency: "USD", Amount: "10"}, Deadline: timestamppb.New(time.Now().Add(10 * time.Minute)),
		})
	}
	restartControl := func() {
		e.start("control")
		e.waitTCP(e.controlAddr)
	}

	var rev1 *mcpv1.Descriptor
	t.Run("discovery through ContextForge binds the live descriptor; a disagreeing listing is refused", func(t *testing.T) {
		_, err := discover("2026-07-28", tools())
		require.Equal(t, codes.FailedPrecondition, status.Code(err), "the connection layer negotiated another protocol version: %v", err)
		require.Contains(t, status.Convert(err).Message(), "DESCRIPTOR_MISMATCH")
		_, err = discover("2025-11-25", tools()[:1])
		require.Equal(t, codes.FailedPrecondition, status.Code(err), "an undeclared live method: %v", err)
		d, err := discover("2025-11-25", tools())
		require.NoError(t, err)
		rev1 = d.GetDescriptor_()
		require.Equal(t, serverID, rev1.GetServerId())
		require.Equal(t, "1", rev1.GetRevision())
		require.Equal(t, mcpv1.CatalogState_CATALOG_STATE_REVIEW_PENDING, rev1.GetState())
		require.Equal(t, "anvilkit-issues-fixture", rev1.GetServerName())
		require.Contains(t, rev1.GetRevisionEvidence(), "contextforge gateway")
		require.Len(t, rev1.GetTools(), 2)
		require.Equal(t, "2025-11-25", rev1.GetProtocolVersion())
	})

	t.Run("four-eyes review and role separation", func(t *testing.T) {
		_, err := catalog.ReviewDescriptor(ctx, &mcpv1.ReviewDescriptorRequest{Command: command("gus"), Scope: scope("gus"), ServerId: serverID, Revision: "1",
			DescriptorDigest: rev1.GetDescriptorDigest(), Decision: mcpv1.ReviewDecision_REVIEW_DECISION_APPROVE})
		require.Equal(t, codes.PermissionDenied, status.Code(err), "a grant manager never reviews")
		_, err = createGrant("gus", rev1)
		require.Equal(t, codes.PermissionDenied, status.Code(err), "an unreviewed revision is never granted: %v", err)
		r, err := catalog.ReviewDescriptor(ctx, &mcpv1.ReviewDescriptorRequest{Command: command("rita"), Scope: scope("rita"), ServerId: serverID, Revision: "1",
			DescriptorDigest: rev1.GetDescriptorDigest(), Decision: mcpv1.ReviewDecision_REVIEW_DECISION_APPROVE})
		require.NoError(t, err)
		require.Equal(t, mcpv1.CatalogState_CATALOG_STATE_APPROVED, r.GetDescriptor_().GetState())
		rev1 = r.GetDescriptor_()
		_, err = createGrant("rita", rev1)
		require.Equal(t, codes.PermissionDenied, status.Code(err), "a reviewer never grants")
		page, err := catalog.ListCatalog(ctx, &mcpv1.ListCatalogRequest{Scope: scope("mia")})
		require.NoError(t, err)
		require.Equal(t, []string{"1:CATALOG_STATE_APPROVED"}, memberView(page.GetDescriptors(), serverID), "a member sees the approved revision")
	})

	var active string
	var sent *controlv1.AdmitToolResponse
	t.Run("a grant is executable only after Control's receipt", func(t *testing.T) {
		g, err := createGrant("gus", rev1)
		require.NoError(t, err)
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_ACTIVE, g.GetGrant().GetState())
		active = g.GetGrant().GetGrantId()
		row := l.query(e.controlDSN, "SELECT policy_digest, receipt_id, revocation_state FROM grant_policies WHERE grant_id = $1", active)
		require.Len(t, row, 1)
		require.Equal(t, g.GetGrant().GetPolicyDigest(), row[0]["policy_digest"])
		require.Equal(t, g.GetGrant().GetControlReceiptId(), row[0]["receipt_id"])
		sent, err = admit(active, "call-active")
		require.NoError(t, err)
		require.True(t, sent.GetAdmission().GetDispatchAllowed(), "the registered grant admits the priced read")
	})

	t.Run("every registration boundary stays non-executable and completes under the original identity", func(t *testing.T) {
		// Control down while the grant is created: PENDING, uncertain.
		e.stop("control")
		g, err := createGrant("gus", rev1)
		require.NoError(t, err)
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_PENDING, g.GetGrant().GetState())
		require.Equal(t, "REGISTRATION_UNCERTAIN", g.GetGrant().GetFailureCode())
		require.True(t, grantState(g.GetGrant().GetGrantId()).GetNewAdmissionBlocked())
		// The registration reaches Control but its answer is lost: Control
		// holds the policy while MCP still has PENDING.
		recorded := l.query(mcpDSN, "SELECT registration_command_id, policy_digest FROM grants WHERE grant_id = $1", g.GetGrant().GetGrantId())
		restartControl()
		direct, err := policy.RegisterPolicy(ctx, &controlv1.RegisterPolicyRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: recorded[0]["registration_command_id"].(string), ActorId: "anvilkit-agent-mcp", RequestDigest: recorded[0]["policy_digest"].(string)},
			GrantId: g.GetGrant().GetGrantId(), GrantRevision: "1", PolicyDigest: recorded[0]["policy_digest"].(string), ServerId: serverID, Methods: []string{"search_issues"},
			CostCap: mcpv1ToControlMoney{}.from(g.GetGrant().GetCostCap()),
		})
		require.NoError(t, err)
		settled := waitGrant(g.GetGrant().GetGrantId(), mcpv1.GrantState_GRANT_STATE_ACTIVE, "the pending grant reconciled")
		require.Equal(t, direct.GetReceiptId(), settled.GetGrant().GetControlReceiptId(), "the retry under the original command answered the same receipt")
		require.Len(t, l.query(e.controlDSN, "SELECT 1 FROM grant_policies WHERE grant_id = $1", g.GetGrant().GetGrantId()), 1)

		// MCP killed with a pending grant: the restarted process completes it.
		e.stop("control")
		g2, err := createGrant("gus", rev1)
		require.NoError(t, err)
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_PENDING, g2.GetGrant().GetState())
		l.procs["mcp"].kill()
		restartControl()
		l.startMCP()
		waitGrant(g2.GetGrant().GetGrantId(), mcpv1.GrantState_GRANT_STATE_ACTIVE, "the restarted MCP registered the pending grant")
	})

	t.Run("revocation: MCP denies at commit, Control fences, REVOKED waits for the sender, across outages", func(t *testing.T) {
		e.stop("control")
		began := time.Now()
		r, err := grants.RevokeGrant(ctx, &mcpv1.RevokeGrantRequest{Command: command("gus"), Scope: scope("gus"), GrantId: active, ExpectedRevision: "1", ReasonCode: proto_("compromised")})
		require.NoError(t, err)
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_REVOKING, r.GetGrant().GetState(), "committed while Control is down")
		p := grantState(active)
		require.True(t, p.GetNewAdmissionBlocked())
		require.False(t, p.GetSendersConverged())
		require.Equal(t, "none", p.GetControlState())
		// MCP crashes while revoking; both come back.
		l.procs["mcp"].kill()
		restartControl()
		l.startMCP()
		l.eventually(2*time.Minute, "Control's fence installed", func() bool { return grantState(active).GetControlState() != "none" })
		p = grantState(active)
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_REVOKING, p.GetGrant().GetState(), "a sender still holds a consumed permission")
		require.Equal(t, "1", p.GetInFlightCalls())
		denied, err := admit(active, "call-after-fence")
		require.NoError(t, err)
		require.False(t, denied.GetAdmission().GetDispatchAllowed(), "Control's fence blocks new admission")
		require.Equal(t, "FORBIDDEN", denied.GetAdmission().GetDenialCode())
		_, err = dispatch.ObserveDispatch(ctx, &controlv1.ObserveDispatchRequest{DispatchId: sent.GetAdmission().GetDispatch().GetDispatchId(), Source: "mcp", Sequence: "1",
			Outcome: controlv1.DispatchOutcome_DISPATCH_OUTCOME_SUCCEEDED, CumulativeUsage: &controlv1.Usage{InputUnits: "1", OutputUnits: "1", ReasoningUnits: "0", CachedInputUnits: "0"}, ObservedAt: timestamppb.Now()})
		require.NoError(t, err)
		done := waitGrant(active, mcpv1.GrantState_GRANT_STATE_REVOKED, "the barrier converged")
		require.True(t, done.GetSendersConverged())
		row := l.query(e.controlDSN, "SELECT revocation_state FROM grant_policies WHERE grant_id = $1", active)
		require.Equal(t, "converged", row[0]["revocation_state"])
		t.Logf("revocation committed to REVOKED in %.1fs across a Control outage and an MCP crash", time.Since(began).Seconds())
	})

	t.Run("a changed server is a new revision grants do not follow; disabling revokes the revision's grants", func(t *testing.T) {
		g, err := createGrant("gus", rev1)
		require.NoError(t, err)
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_ACTIVE, g.GetGrant().GetState())
		fixture.start(2)
		d, err := discover("2025-11-25", tools())
		require.NoError(t, err)
		rev2 := d.GetDescriptor_()
		require.Equal(t, "2", rev2.GetRevision())
		require.NotEqual(t, rev1.GetDescriptorDigest(), rev2.GetDescriptorDigest())
		require.Equal(t, mcpv1.CatalogState_CATALOG_STATE_REVIEW_PENDING, rev2.GetState())
		_, err = createGrant("gus", rev2)
		require.Equal(t, codes.PermissionDenied, status.Code(err))
		page, err := catalog.ListCatalog(ctx, &mcpv1.ListCatalogRequest{Scope: scope("mia")})
		require.NoError(t, err)
		require.Equal(t, []string{"1:CATALOG_STATE_APPROVED"}, memberView(page.GetDescriptors(), serverID), "a member never sees the pending revision")
		page, err = catalog.ListCatalog(ctx, &mcpv1.ListCatalogRequest{Scope: scope("rita")})
		require.NoError(t, err)
		require.Equal(t, []string{"1:CATALOG_STATE_APPROVED", "2:CATALOG_STATE_REVIEW_PENDING"}, memberView(page.GetDescriptors(), serverID), "a reviewer sees it")
		still := grantState(g.GetGrant().GetGrantId()).GetGrant()
		require.Equal(t, rev1.GetDescriptorDigest(), still.GetDescriptorDigest(), "the grant stays bound to the revision it names")
		_, err = catalog.DisableDescriptor(ctx, &mcpv1.DisableDescriptorRequest{Command: command("rita"), Scope: scope("rita"), ServerId: serverID, Revision: "1", ReasonCode: proto_("schema-drift")})
		require.NoError(t, err)
		waitGrant(g.GetGrant().GetGrantId(), mcpv1.GrantState_GRANT_STATE_REVOKED, "the disabled revision's grant revoked")
		denied, err := admit(g.GetGrant().GetGrantId(), "call-disabled")
		require.NoError(t, err)
		require.False(t, denied.GetAdmission().GetDispatchAllowed())
		// One ContextForge connection per resource, refreshed on rediscovery.
		token, err := os.ReadFile(tokenFile)
		require.NoError(t, err)
		req, _ := http.NewRequest(http.MethodGet, cfURL+"/v1/gateways", nil)
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(string(token)))
		resp, err := http.DefaultClient.Do(req)
		require.NoError(t, err)
		defer resp.Body.Close()
		var gws []map[string]any
		require.NoError(t, json.NewDecoder(resp.Body).Decode(&gws))
		require.Len(t, gws, 1)
	})
}

// memberView lists one server's revisions and states in a catalog page (the
// foundation's database also holds other scenarios' descriptors).
func memberView(ds []*mcpv1.Descriptor, serverID string) []string {
	var out []string
	for _, d := range ds {
		if d.GetServerId() == serverID {
			out = append(out, d.GetRevision()+":"+d.GetState().String())
		}
	}
	sort.Strings(out)
	return out
}

// mcpv1ToControlMoney converts the MCP contract's money to Control's.
type mcpv1ToControlMoney struct{}

func (mcpv1ToControlMoney) from(m *mcpv1.Money) *controlv1.Money {
	return &controlv1.Money{Currency: m.GetCurrency(), Amount: m.GetAmount()}
}

func proto_(s string) *string { return &s }
