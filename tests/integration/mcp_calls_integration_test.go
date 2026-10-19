//go:build integration

package integration

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	mcpv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/mcp/v1"
)

// P19 on the development foundation: guarded tool execution across real
// processes — MCP (its lane on the foundation's MCP database), a real
// Control, a disposable ContextForge v1.0.11 for discovery and the
// official-SDK fixture server with its own authorization server. Every
// upstream receipt of tools/call is counted by the fixture. Asserted: a
// paid read and a free write each pass Control's AdmitTool (with the
// argument digest and effect class in the dispatch) and reach the
// upstream exactly once under a resource-bound token obtained through the
// protected-resource metadata the reviewed descriptor bound, never under
// the inbound AnvilKit credential; a repeated command sends nothing;
// hostile results stay bounded data; schema drift after review is refused
// before the send; a connection lost after receipt is UNKNOWN, never
// resent, and restricts new calls to the server (a replacement id
// included) across an MCP restart, since the server has no original-outcome
// query; a revoked grant admits no call and stays REVOKING while its
// UNKNOWN call is open.

func digestOfBytes(b []byte) string { return fmt.Sprintf("sha256:%x", sha256.Sum256(b)) }

type fixtureCounts struct {
	Received map[string]int  `json:"received"`
	Auth     map[string]bool `json:"auth"`
	Protos   map[string]bool `json:"protos"`
}

func TestMCPToolCalls(t *testing.T) {
	laneEnv(t, "ANVILKIT_DEV_CONTROL_DSN", "ANVILKIT_DEV_TEMPORAL_ADDRESS")
	root := repoRoot(t)
	dir := t.TempDir()
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	// The upstream fixture with its authorization server.
	secretFile := filepath.Join(dir, "client-secret")
	require.NoError(t, os.WriteFile(secretFile, []byte(randomHex(16)), 0o600))
	asAddr := "127.0.0.1:" + freePort(t)
	fixture := &fixtureServer{t: t, bin: filepath.Join(dir, "mcpfixture"), addr: "127.0.0.1:" + freePort(t), extra: []string{"-as-listen", asAddr, "-client-secret-file", secretFile}}
	build := exec.Command("go", "build", "-o", fixture.bin, "./cmd/mcpfixture")
	build.Dir = filepath.Join(root, "tests", "integration")
	out, err := build.CombinedOutput()
	require.NoError(t, err, string(out))
	fixture.start(3)
	t.Cleanup(fixture.stop)
	resource := "http://" + fixture.addr + "/mcp"
	issuer := "http://" + asAddr
	serverID := serverIDOf("tenant_a", resource)
	cfURL, tokenFile := ownContextForge(t, dir)
	counts := func() fixtureCounts {
		resp, err := http.Get("http://" + fixture.addr + "/counts")
		require.NoError(t, err)
		defer resp.Body.Close()
		var c fixtureCounts
		require.NoError(t, json.NewDecoder(resp.Body).Decode(&c))
		return c
	}

	// Control with a paid read, a free write and a paid hostile read.
	base := dispatchControlConfig(t, root, dir)
	raw, err := os.ReadFile(base)
	require.NoError(t, err)
	text := string(raw)
	start, end := strings.Index(text, "      - revision: fx-int-tool\n"), strings.Index(text, "    authorized_routes:")
	require.True(t, start > 0 && end > start)
	price := func(rev, method, perMillion string) string {
		return "      - revision: " + rev + "\n        kind: tool\n        route: " + serverID + "/" + method + "\n        currency: USD\n" +
			"        effective_from: 2026-01-01T00:00:00Z\n        effective_until: 2027-01-01T00:00:00Z\n" +
			"        per_million: {input_units: \"" + perMillion + "\", output_units: \"0\", reasoning_units: \"0\", cached_input_units: \"0\"}\n        max_exposure: \"1000\"\n"
	}
	text = text[:start] + price("p19-read", "search_issues", "1000000") + price("p19-write", "create_issue", "0") + price("p19-inspect", "inspect", "1000000") +
		price("p1-2-big", "big", "1000000") + price("p1-2-html", "html", "1000000") + text[end:]
	// P2.1: MCP asks Control's AuthorizationService before every call; the
	// DEVELOPMENT_ONLY fixture allows a resource other than a model route only
	// for a tenant listed with the route "*" (quoted: a bare * is a YAML alias).
	text = strings.Replace(text, "        route_id: fixture-route", "        route_id: fixture-route\n      - tenant_id: tenant_a\n        route_id: \"*\"", 1)
	controlCfg := filepath.Join(dir, "control.calls.yaml")
	require.NoError(t, os.WriteFile(controlCfg, []byte(text), 0o600))
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

	// MCP with the qualified route and the upstream credential binding.
	roles := filepath.Join(dir, "roles.json")
	bindings, _ := json.Marshal([]map[string]string{
		{"tenantId": "tenant_a", "actorId": "carol", "role": "catalog_manager"},
		{"tenantId": "tenant_a", "actorId": "rita", "role": "catalog_reviewer"},
		{"tenantId": "tenant_a", "actorId": "gus", "role": "grant_manager"},
	})
	require.NoError(t, os.WriteFile(roles, bindings, 0o600))
	creds := filepath.Join(dir, "upstream-credentials.json")
	credJSON, _ := json.Marshal([]map[string]any{{"resource": resource, "issuer": issuer, "clientId": "mcp-a", "clientSecretFile": secretFile}})
	require.NoError(t, os.WriteFile(creds, credJSON, 0o600))
	routes := fmt.Sprintf("  qualified_routes:\n    - {resource: %q, protocol_version: \"2025-11-25\", transport: streamable-http, route: go-sdk}", resource)
	l := setupLaneWith(t, laneOptions{
		mcpReplacements: [][2]string{{"  private_hosts: []", `  private_hosts: ["127.0.0.1"]`}, {"  qualified_routes: []", routes},
			{"  send_timeout: 60s", "  send_timeout: 10s"}, {"  wait: 30s", "  wait: 20s"}, {"  reconcile_age: 2m", "  reconcile_age: 15s"}},
		mcpEnv: []string{"ANVILKIT_MCP_CONTROL_ADDRESS=" + e.controlAddr, "ANVILKIT_MCP_CONTEXTFORGE_URL=" + cfURL, "ANVILKIT_MCP_CONTEXTFORGE_TOKEN_FILE=" + tokenFile,
			"ANVILKIT_MCP_ROLES_FILE=" + roles, "ANVILKIT_MCP_UPSTREAM_CREDENTIALS_FILE=" + creds},
	})
	mcpConn := dialMCPAs(t, l.mcpAddr, principalAPI)
	catalog, grants, calls := mcpv1.NewCatalogServiceClient(mcpConn), mcpv1.NewGrantServiceClient(mcpConn), mcpv1.NewCallServiceClient(mcpConn)
	ops, execs := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))

	seq := 0
	command := func(actor string) *mcpv1.CommandIdentity {
		seq++
		id := fmt.Sprintf("p19-%s-%d", run, seq)
		return &mcpv1.CommandIdentity{TenantId: "tenant_a", CommandId: id, ActorId: actor, RequestDigest: digestOf(id)}
	}
	scope := func(actor string) *mcpv1.Scope {
		return &mcpv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: actor}
	}

	// Discovery binds the issuer the resource names; review and grant.
	d, err := catalog.DiscoverServer(ctx, &mcpv1.DiscoverServerRequest{Command: command("carol"), Scope: scope("carol"), CanonicalResource: resource,
		Transport: "streamable-http", ProtocolVersion: "2025-11-25", Provenance: "fixture:anvilkit-issues-v3", DataClass: "internal",
		Tools: []*mcpv1.ToolSchema{
			{Method: "search_issues", UnitPrice: &mcpv1.Money{Currency: "USD", Amount: "1"}, Idempotent: true},
			{Method: "create_issue", SideEffecting: true, UnitPrice: &mcpv1.Money{Currency: "USD", Amount: "0"}},
			{Method: "inspect", UnitPrice: &mcpv1.Money{Currency: "USD", Amount: "1"}},
			{Method: "big", UnitPrice: &mcpv1.Money{Currency: "USD", Amount: "1"}},
			{Method: "html", UnitPrice: &mcpv1.Money{Currency: "USD", Amount: "1"}},
		}})
	require.NoError(t, err)
	desc := d.GetDescriptor_()
	r, err := catalog.ReviewDescriptor(ctx, &mcpv1.ReviewDescriptorRequest{Command: command("rita"), Scope: scope("rita"), ServerId: serverID, Revision: desc.GetRevision(),
		DescriptorDigest: desc.GetDescriptorDigest(), Decision: mcpv1.ReviewDecision_REVIEW_DECISION_APPROVE})
	require.NoError(t, err)
	desc = r.GetDescriptor_()
	g, err := grants.CreateGrant(ctx, &mcpv1.CreateGrantRequest{Command: command("gus"), Scope: scope("gus"), SubjectType: "project", SubjectId: "proj_a",
		ServerId: serverID, DescriptorRevision: desc.GetRevision(), DescriptorDigest: desc.GetDescriptorDigest(), Methods: []string{"search_issues", "create_issue", "inspect", "big", "html"},
		Purpose: "triage", CostCap: &mcpv1.Money{Currency: "USD", Amount: "1000"}})
	require.NoError(t, err)
	require.Equal(t, mcpv1.GrantState_GRANT_STATE_ACTIVE, g.GetGrant().GetState())
	grant := g.GetGrant()
	require.Equal(t, issuer, grant.GetIssuer(), "the reviewed revision, and so the grant, binds the issuer the protected-resource metadata names")
	opID, attemptID := e.fundedOperation(ctx, t, ops, execs, run, "calls", 100_000)

	// Every call carries an inbound AnvilKit credential that must never
	// reach the upstream.
	inbound := metadata.AppendToOutgoingContext(ctx, "authorization", "Bearer anvilkit-inbound-"+run)
	call := func(name, method, args string) (*mcpv1.CreateCallResponse, error) {
		return calls.CreateCall(inbound, &mcpv1.CreateCallRequest{
			Command: &mcpv1.CommandIdentity{TenantId: "tenant_a", CommandId: "call-" + run + "-" + name, ActorId: "user_a", RequestDigest: digestOf(name + args)},
			Scope:   scope("user_a"), GrantId: grant.GetGrantId(), GrantRevision: grant.GetRevision(), Method: method, ArgumentRef: "inline",
			ArgumentDigest: digestOfBytes([]byte(args)), Arguments: []byte(args), OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1",
			Deadline: timestamppb.New(time.Now().Add(10 * time.Minute)),
		})
	}
	dispatchRow := func(callID string) map[string]any {
		rows := l.query(e.controlDSN, "SELECT state, outcome, argument_digest, side_effecting, reserved_amount FROM dispatches WHERE owner = 'anvilkit-agent-mcp' AND call_id = $1", callID)
		require.Len(t, rows, 1)
		return rows[0]
	}
	noInbound := func() {
		for h := range counts().Auth {
			require.NotContains(t, h, "anvilkit-inbound", "an inbound credential reached the upstream")
		}
	}

	t.Run("a paid read is admitted once and sent once under the resource-bound token", func(t *testing.T) {
		resp, err := call("read", "search_issues", `{"query":"open"}`)
		require.NoError(t, err)
		c := resp.GetCall()
		require.Equal(t, mcpv1.CallState_CALL_STATE_SUCCEEDED, c.GetState(), c.GetFailureCode())
		require.Contains(t, string(c.GetResult()), "0 issues")
		require.Equal(t, digestOfBytes(c.GetResult()), c.GetResultDigest())
		require.Equal(t, 1, counts().Received["search_issues"])
		row := dispatchRow(c.GetCallId())
		require.Equal(t, "observed", row["state"])
		require.Equal(t, digestOfBytes([]byte(`{"query":"open"}`)), row["argument_digest"], "the obligation binds the exact arguments")
		require.Equal(t, false, row["side_effecting"])
		auth := counts().Auth
		tokened := false
		for h := range auth {
			tokened = tokened || strings.HasPrefix(h, "Bearer fixture-token-")
		}
		require.True(t, tokened, "the send carried the token the fixture's authorization server issued for this resource")
		noInbound()
		again, err := call("read", "search_issues", `{"query":"open"}`)
		require.NoError(t, err)
		require.True(t, again.GetExisting())
		require.Equal(t, c.GetCallId(), again.GetCall().GetCallId())
		require.Equal(t, 1, counts().Received["search_issues"], "a repeated command never sends again")
	})

	// P0.3 AC4: a grant issued to a role subject sends for a caller whose
	// verified roles (carried by the API workload) name the role, and for
	// no one else.
	t.Run("a grant to a role subject sends for a holder of the verified role only", func(t *testing.T) {
		rg, err := grants.CreateGrant(ctx, &mcpv1.CreateGrantRequest{Command: command("gus"), Scope: scope("gus"), SubjectType: "role", SubjectId: "reviewer",
			ServerId: serverID, DescriptorRevision: desc.GetRevision(), DescriptorDigest: desc.GetDescriptorDigest(), Methods: []string{"search_issues"},
			Purpose: "review", CostCap: &mcpv1.Money{Currency: "USD", Amount: "1000"}})
		require.NoError(t, err)
		roleGrant := rg.GetGrant()
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_ACTIVE, roleGrant.GetState())
		roleCall := func(name string, roles []string) (*mcpv1.CreateCallResponse, error) {
			args := `{"query":"role ` + name + `"}`
			return calls.CreateCall(inbound, &mcpv1.CreateCallRequest{
				Command: &mcpv1.CommandIdentity{TenantId: "tenant_a", CommandId: "call-" + run + "-" + name, ActorId: "user_r", RequestDigest: digestOf(name)},
				Scope:   &mcpv1.Scope{TenantId: "tenant_a", ProjectId: "proj_r", ActorId: "user_r", Roles: roles}, GrantId: roleGrant.GetGrantId(), GrantRevision: roleGrant.GetRevision(),
				Method: "search_issues", ArgumentRef: "inline", ArgumentDigest: digestOfBytes([]byte(args)), Arguments: []byte(args), OperationId: opID, AttemptId: attemptID,
				ExecutionEpoch: "1", Deadline: timestamppb.New(time.Now().Add(10 * time.Minute)),
			})
		}
		before := counts().Received["search_issues"]
		_, err = roleCall("role-none", []string{"author"})
		require.Equal(t, codes.NotFound, status.Code(err), "a caller without the role does not see the grant: %v", err)
		resp, err := roleCall("role-held", []string{"author", "reviewer"})
		require.NoError(t, err)
		require.Equal(t, mcpv1.CallState_CALL_STATE_SUCCEEDED, resp.GetCall().GetState(), resp.GetCall().GetFailureCode())
		require.Equal(t, before+1, counts().Received["search_issues"], "the role holder's call was sent once; the refused one was not")
		noInbound()
	})

	t.Run("a free write is admitted and inventoried like a paid one", func(t *testing.T) {
		resp, err := call("write", "create_issue", `{"title":"ok"}`)
		require.NoError(t, err)
		c := resp.GetCall()
		require.Equal(t, mcpv1.CallState_CALL_STATE_SUCCEEDED, c.GetState(), c.GetFailureCode())
		row := dispatchRow(c.GetCallId())
		require.Equal(t, true, row["side_effecting"])
		require.Equal(t, "observed", row["state"])
		require.EqualValues(t, 0, row["reserved_amount"])
		require.Equal(t, 1, counts().Received["create_issue"])
	})

	t.Run("hostile results stay bounded data", func(t *testing.T) {
		resp, err := call("inspect", "inspect", `{"target":"x"}`)
		require.NoError(t, err)
		c := resp.GetCall()
		require.Equal(t, mcpv1.CallState_CALL_STATE_SUCCEEDED, c.GetState(), c.GetFailureCode())
		var res struct {
			Untrusted bool             `json:"untrusted"`
			Content   []map[string]any `json:"content"`
		}
		require.NoError(t, json.Unmarshal(c.GetResult(), &res))
		require.True(t, res.Untrusted)
		require.Equal(t, "resource_link", res.Content[1]["type"])
		require.Equal(t, "image", res.Content[2]["type"])
		require.NotContains(t, string(c.GetResult()), "fixture-bytes", "binary content is reduced to its digest")
		require.Equal(t, 1, counts().Received["inspect"], "the embedded instruction caused no further call")
		require.Equal(t, 1, counts().Received["create_issue"])
	})

	t.Run("schema drift after review is refused before the send", func(t *testing.T) {
		fixture.start(4)
		resp, err := call("drift", "search_issues", `{"query":"drift"}`)
		require.NoError(t, err)
		c := resp.GetCall()
		require.Equal(t, mcpv1.CallState_CALL_STATE_CANCELED, c.GetState())
		require.Equal(t, "SCHEMA_DRIFT", c.GetFailureCode())
		require.Equal(t, 0, counts().Received["search_issues"], "the drifted server received no call")
		row := dispatchRow(c.GetCallId())
		require.Equal(t, "observed", row["state"])
		require.Equal(t, "canceled", row["outcome"])
		fixture.start(3)
	})

	t.Run("P1.2: results the record cannot hold settle FAILED/RESULT_TOO_LARGE with usage; the server is not restricted; the send route is HTTP/1.1", func(t *testing.T) {
		for _, method := range []string{"big", "html"} {
			resp, err := call("too-large-"+method, method, `{"target":"x"}`)
			require.NoError(t, err, method)
			c := resp.GetCall()
			require.Equal(t, mcpv1.CallState_CALL_STATE_FAILED, c.GetState(), "%s: %s", method, c.GetFailureCode())
			require.Equal(t, "RESULT_TOO_LARGE", c.GetFailureCode(), method)
			require.Equal(t, 1, counts().Received[method], method)
			row := dispatchRow(c.GetCallId())
			require.Equal(t, "observed", row["state"], method)
			require.Equal(t, "failed", row["outcome"], method)
			usage := l.query(e.controlDSN, "SELECT usage_reported FROM usage_observations WHERE dispatch_id = (SELECT dispatch_id FROM dispatches WHERE owner = 'anvilkit-agent-mcp' AND call_id = $1)", c.GetCallId())
			require.Len(t, usage, 1, method)
			require.Equal(t, true, usage[0]["usage_reported"], "%s: the usage of the delivered answer reaches the ledger", method)
		}
		after, err := call("after-too-large", "search_issues", `{"query":"still open"}`)
		require.NoError(t, err)
		require.Equal(t, mcpv1.CallState_CALL_STATE_SUCCEEDED, after.GetCall().GetState(), "a definite failure restricts nothing")
		require.Equal(t, map[string]bool{"HTTP/1.1": true}, counts().Protos, "every tools/call arrived over HTTP/1.1: no h2 replay path exists")
	})

	t.Run("a connection lost after receipt is UNKNOWN, never resent, and restricts the server", func(t *testing.T) {
		resp, err := call("drop", "create_issue", `{"title":"drop"}`)
		require.NoError(t, err)
		c := resp.GetCall()
		require.Equal(t, mcpv1.CallState_CALL_STATE_UNKNOWN, c.GetState(), c.GetFailureCode())
		require.Equal(t, "unknown", dispatchRow(c.GetCallId())["state"])
		_, err = call("drop-replacement", "create_issue", `{"title":"drop"}`)
		require.Equal(t, codes.PermissionDenied, status.Code(err), "a replacement id after UNKNOWN: %v", err)
		require.Contains(t, status.Convert(err).Message(), "RECOVERY_RESTRICTED")
		_, err = call("read-after-unknown", "search_issues", `{"query":"blocked"}`)
		require.Equal(t, codes.PermissionDenied, status.Code(err))
		l.procs["mcp"].kill()
		l.startMCP()
		time.Sleep(20 * time.Second) // beyond reconcile_age: the reconciler runs over the UNKNOWN call
		got, err := calls.GetCall(ctx, &mcpv1.GetCallRequest{Scope: scope("user_a"), CallId: c.GetCallId()})
		require.NoError(t, err)
		require.Equal(t, mcpv1.CallState_CALL_STATE_UNKNOWN, got.GetCall().GetState(), "no original-outcome query: recovery stays manual")
		require.Equal(t, 1, counts().Received["create_issue"], "the lost call was received once and never sent again")
		_, err = call("drop-after-restart", "create_issue", `{"title":"drop"}`)
		require.Equal(t, codes.PermissionDenied, status.Code(err))
		noInbound()

		// P1.2 (AC6): the operator path. Control resolves the original identity
		// (here: the operator's disposition recorded on the dispatch row as a
		// definite failed outcome; the Control-side RPC for it is P1.5), then
		// `anvilkit-agent-mcp reconcile <callId>` advances the call from that
		// record and the server's restriction is cleared. Nothing is sent.
		before := counts().Received["create_issue"]
		_, err = pools.Exec(ctx, "UPDATE dispatches SET state = 'observed', outcome = 'failed' WHERE owner = 'anvilkit-agent-mcp' AND call_id = $1 AND state = 'unknown'", c.GetCallId())
		require.NoError(t, err)
		out, err := l.mcpCommand("reconcile", c.GetCallId())
		require.NoError(t, err, string(out))
		var result struct {
			State       string `json:"state"`
			FailureCode string `json:"failureCode"`
		}
		require.NoError(t, json.Unmarshal(out, &result), string(out))
		require.Equal(t, "failed", result.State)
		require.Equal(t, "RESOLVED_failed", result.FailureCode)
		got, err = calls.GetCall(ctx, &mcpv1.GetCallRequest{Scope: scope("user_a"), CallId: c.GetCallId()})
		require.NoError(t, err)
		require.Equal(t, mcpv1.CallState_CALL_STATE_FAILED, got.GetCall().GetState())
		require.Equal(t, before, counts().Received["create_issue"], "the operator path never sends")
		cleared, err := call("after-operator-reconcile", "create_issue", `{"title":"cleared"}`)
		require.NoError(t, err, "the restriction is cleared once the original identity is resolved")
		require.Equal(t, mcpv1.CallState_CALL_STATE_SUCCEEDED, cleared.GetCall().GetState())
		// The next scenario needs an open UNKNOWN call on the server: another lost connection.
		resp, err = call("drop-2", "create_issue", `{"title":"drop"}`)
		require.NoError(t, err)
		require.Equal(t, mcpv1.CallState_CALL_STATE_UNKNOWN, resp.GetCall().GetState())
		_, err = call("after-drop-2", "search_issues", `{"query":"blocked again"}`)
		require.Equal(t, codes.PermissionDenied, status.Code(err))
	})

	t.Run("a revoked grant admits no call and waits for its UNKNOWN call", func(t *testing.T) {
		before := counts().Received["inspect"] // the fixture's counters restarted with it
		rv, err := grants.RevokeGrant(ctx, &mcpv1.RevokeGrantRequest{Command: command("gus"), Scope: scope("gus"), GrantId: grant.GetGrantId(), ExpectedRevision: grant.GetRevision(), ReasonCode: proto_("p19")})
		require.NoError(t, err)
		require.Equal(t, mcpv1.GrantState_GRANT_STATE_REVOKING, rv.GetGrant().GetState())
		_, err = call("after-revoke", "inspect", `{"target":"y"}`)
		require.Equal(t, codes.PermissionDenied, status.Code(err))
		p, err := grants.GetRevocationProgress(ctx, &mcpv1.GetRevocationProgressRequest{Scope: scope("gus"), GrantId: grant.GetGrantId()})
		require.NoError(t, err)
		require.True(t, p.GetNewAdmissionBlocked())
		require.False(t, p.GetSendersConverged(), "the UNKNOWN call keeps the barrier open")
		require.Equal(t, before, counts().Received["inspect"], "nothing reached the upstream under the revoked grant")
	})
}
