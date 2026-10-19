//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.yaml.in/yaml/v3"
)

// TestPreparationAndGenerationLifecycle runs the P13 lifecycles on the
// disposable dependencies with real processes: the API, one Control, one
// Workflow worker, one Model Proxy against the countable controlled
// upstream, real PostgreSQL and Temporal, a private MinIO as the
// artifact store, and the DEVELOPMENT_ONLY Pagix/Knowledge doubles.
//
// Preparation: a prompt artifact is accepted into a preparation; the
// analysis asks one grouped question through the Proxy (one physical
// send); the answer submitted through API-06 reaches the run as its
// tracked Update; the second analysis concludes; the brief is frozen as
// an artifact and the preparation succeeds with its briefId. Generation:
// an operation against the brief is admitted (permit, active deadline),
// its scope checked, funded and leased under stable occurrences, and its
// codegen launch is refused by the launcher because the development team
// profile (state development since P2.4) is not enabled in this
// environment (kubernetes.enabled_profiles): the operation fails
// PROFILE_UNQUALIFIED with its lease released and its permit freed.
// Nothing here claims candidate readiness: that needs a qualified Job
// environment.
func TestPreparationAndGenerationLifecycle(t *testing.T) {
	isolatedControlDatabase(t)
	root := repoRoot(t)
	scratch := t.TempDir()
	port := freePort(t)
	proxyURL, healthURL := "http://127.0.0.1:"+port, "http://127.0.0.1:"+freePort(t)
	up := newModelUpstream(t)
	dist := buildModelProxy(t, root)
	principals := filepath.Join(scratch, "model-proxy-principals.json")
	require.NoError(t, os.WriteFile(principals, []byte(fmt.Sprintf(`{%q:{"principalId":"anvilkit-agent-workflow","kind":"workflow"},%q:{"principalId":"anvilkit-job-access-sidecar","kind":"sidecar"},%q:{"principalId":"anvilkit-agent-control","kind":"control"}}`, proxyTokenWorkflow, proxyTokenSidecar, proxyTokenControl)), 0o600))
	doubles := filepath.Join(scratch, "pagix-double")
	controlConfig := modelProxyControlConfig(t, root, scratch)
	configBytes, err := os.ReadFile(controlConfig)
	require.NoError(t, err)
	// Each analysis reserves the reviewed full fixture route exposure. Fund
	// both rounds plus incurred costs; retain the real per-send cost gate.
	var controlValues map[string]any
	require.NoError(t, yaml.Unmarshal(configBytes, &controlValues))
	controlValues["profiles"].(map[string]any)["preparation"] = map[string]any{"funding": map[string]any{"currency": "USD", "amount": "5000000"}}
	// P1.6 AC2: the dispatch and settlement reconcilers run on a short
	// interval here, so a settlement Control re-applies is seen in seconds.
	controlValues["dispatch"].(map[string]any)["reconcile_interval"] = "2s"
	controlValues["dispatch"].(map[string]any)["reconcile_age"] = "1m"
	configBytes, err = yaml.Marshal(controlValues)
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(controlConfig, configBytes, 0o600))
	// The harness's Model Proxy runs the DEVELOPMENT_ONLY bearer identity
	// (writeProxyConfig): the Workflow presents its bearer token there
	// instead of the mTLS default of its configuration.
	workflowRaw, err := os.ReadFile(filepath.Join(root, "services", "agent", "workflow", "config.yaml"))
	require.NoError(t, err)
	var workflowValues map[string]any
	require.NoError(t, yaml.Unmarshal(workflowRaw, &workflowValues))
	workflowValues["model_proxy"].(map[string]any)["identity"] = map[string]any{"mode": "development"}
	workflowRaw, err = yaml.Marshal(workflowValues)
	require.NoError(t, err)
	workflowConfig := filepath.Join(scratch, "workflow.yaml")
	require.NoError(t, os.WriteFile(workflowConfig, workflowRaw, 0o600))
	e := setupProcessesFull(t, false, workflowConfig, controlConfig, false,
		[]string{"ANVILKIT_CONTROL_MODEL_PROXY_ADDRESS=" + proxyURL, "ANVILKIT_CONTROL_MODEL_PROXY_TOKEN=" + proxyTokenControl},
		[]string{"ANVILKIT_WORKFLOW_MODEL_PROXY_ADDRESS=" + proxyURL, "ANVILKIT_WORKFLOW_MODEL_PROXY_TOKEN=" + proxyTokenWorkflow, "ANVILKIT_WORKFLOW_PAGIX_DOUBLE_DIR=" + doubles})
	proxy := &proxyProcess{name: "proxy", url: proxyURL, healthURL: healthURL, port: port, storeDir: filepath.Join(scratch, "store"), cfg: writeProxyConfig(t, root, scratch, "127.0.0.1:"+port, up.srv.URL), principals: principals, dist: dist, controlAddr: e.controlAddr, root: root, t: t}
	proxy.start()
	t.Cleanup(func() {
		proxy.stop()
		if t.Failed() {
			t.Logf("proxy log:\n%s\nworkflow log:\n%s\ncontrol log:\n%s", logTail(t, filepath.Join(scratch, "proxy.log"), 40), logTail(t, filepath.Join(e.local, "workflow.log"), 60), logTail(t, filepath.Join(e.local, "control.log"), 40))
		}
	})
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
	defer cancel()
	conn := e.db()
	for _, row := range []struct {
		level         string
		tenant, actor *string
		cap           int64
	}{{"platform", nil, nil, 1_000_000_000}, {"tenant", strPtr("tenant_a"), nil, 100_000_000}, {"actor", strPtr("tenant_a"), strPtr("user_a"), 20_000_000}} {
		_, err := conn.Exec(ctx, "INSERT INTO budget_pools (pool_id, level, tenant_id, actor_id, period_start, period_end, currency, cap_amount) VALUES ($1, $2, $3, $4, now() - interval '1 hour', now() + interval '1 hour', 'USD', $5)",
			"pool-"+run+"-"+row.level, row.level, row.tenant, row.actor, row.cap)
		require.NoError(t, err)
	}
	t.Cleanup(func() {
		_, _ = conn.Exec(context.Background(), "UPDATE budget_pools SET period_end = period_start + interval '1 second' WHERE pool_id LIKE $1", "pool-"+run+"-%")
	})

	// finalizedArtifact moves bytes through API-12: begin, the one PUT the capability authorizes, finalize.
	finalizedArtifact := func(id, class, mediaType string, body []byte) (transferID, digest string) {
		t.Helper()
		code, out := e.call("POST", "/api/v1/artifacts/transfers", tokenA, fmt.Sprintf(`{"commandId":"xfer-%s-%s","class":"%s","expectedDigest":"%s","expectedSize":"%d","mediaType":"%s"}`, run, id, class, digestOf(string(body)), len(body), mediaType))
		require.Equal(t, 201, code, "%v", out)
		version, status := upload(t, out, body)
		require.Equal(t, 200, status)
		code, fin := e.call("POST", "/api/v1/artifacts/"+out["handle"].(string)+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-`+id+`","objectVersion":"`+version+`"}`)
		require.Equal(t, 200, code, "%v", fin)
		return fin["transferId"].(string), digestOf(string(body))
	}
	await := func(operationID string, done func(view map[string]any) bool, within time.Duration) map[string]any {
		t.Helper()
		deadline := time.Now().Add(within)
		var view map[string]any
		for time.Now().Before(deadline) {
			code, out := e.call("GET", "/api/v1/operations/"+operationID, tokenA, "")
			require.Equal(t, 200, code, "%v", out)
			view = out
			if done(out) {
				return out
			}
			time.Sleep(300 * time.Millisecond)
		}
		t.Fatalf("operation %s did not reach the expected state within %s; last view: %v", operationID, within, view)
		return nil
	}

	// The analysis: one grouped question, then the requirements.
	up.next(
		upstreamBehavior{kind: "stream", tool: []string{"tc_1", "ask_questions", `{"questions":[{"id":"q1","text":"Which call to action should the hero carry?"}]}`}},
		upstreamBehavior{kind: "stream", tool: []string{"tc_2", "submit_requirements", `{"componentName":"Hero","packageName":"@acme/hero","purpose":"A hero section","content":"Headline, subheading and one call to action","interaction":"","editableFields":[{"name":"headline","type":"text"}],"constraints":[],"acceptance":["renders the headline"],"unknowns":[]}`}},
	)
	promptTransfer, promptDigest := finalizedArtifact("prompt", "prompt", "text/plain", []byte("A hero section for a launch page, run "+run+"\n"))
	code, prep := e.call("POST", "/api/v1/preparations", tokenA, fmt.Sprintf(`{"commandId":"prep-%s","prompt":{"transferId":"%s","digest":"%s"},"brandReferences":[{"sourceId":"brand_%s","revision":"1"}]}`, run, promptTransfer, promptDigest, run))
	require.Equal(t, 202, code, "%v", prep)
	prepID := prep["operationId"].(string)
	require.Equal(t, "preparation", prep["kind"])
	code, again := e.call("POST", "/api/v1/preparations", tokenA, fmt.Sprintf(`{"commandId":"prep-%s","prompt":{"transferId":"%s","digest":"%s"},"brandReferences":[{"sourceId":"brand_%s","revision":"1"}]}`, run, promptTransfer, promptDigest, run))
	require.Equal(t, 202, code)
	require.Equal(t, prepID, again["operationId"], "the same command returns the original preparation")

	waiting := await(prepID, func(v map[string]any) bool { return v["lifecycle"] == "waiting" && v["clarification"] != nil }, 2*time.Minute)
	clarification := waiting["clarification"].(map[string]any)
	questionSetID := clarification["questionSetId"].(string)
	require.Equal(t, "1", clarification["round"])
	questions := clarification["questions"].([]any)
	require.Len(t, questions, 1)
	require.Equal(t, int32(1), up.receives.Load(), "one physical analysis send so far")
	askedAt, _ := time.Parse(time.RFC3339Nano, clarification["askedAt"].(string))
	expiresAt, _ := time.Parse(time.RFC3339Nano, clarification["expiresAt"].(string))
	require.Equal(t, 7*24*time.Hour, expiresAt.Sub(askedAt), "the seven-day absolute wait is recorded with the question set")
	var attemptsOpen int
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM attempts WHERE operation_id = $1 AND state <> 'closed'", prepID).Scan(&attemptsOpen))
	require.Equal(t, 0, attemptsOpen, "waiting holds no attempt")
	var reserved int64
	require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(max(reserved), 0) FROM allocations WHERE operation_id = $1", prepID).Scan(&reserved))
	require.Equal(t, int64(0), reserved, "waiting holds no spendable reservation; the incurred cost stays in the ledger")
	var charged int64
	require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(sum(amount), 0) FROM cost_entries WHERE operation_id = $1 AND kind = 'actual'", prepID).Scan(&charged))
	require.Greater(t, charged, int64(0), "the analysis call's cost is in the ledger")

	// A hold on a preparation is rejected by its profile; cancellation stays available (not exercised here).
	code, hold := e.call("POST", "/api/v1/operations/"+prepID+"/commands", tokenA, fmt.Sprintf(`{"commandId":"hold-%s","kind":"hold","expectedRevision":"%s"}`, run, waiting["revision"]))
	require.Equal(t, 202, code, "%v", hold)
	require.Equal(t, "rejected", hold["outcome"])
	require.Equal(t, "UNSUPPORTED_BY_PROFILE", hold["reasonCode"])

	answerTransfer, answerDigest := finalizedArtifact("answer", "answer", "application/json", []byte(`{"q1":"Book a demo"}`))
	code, stale := e.call("POST", "/api/v1/preparations/"+prepID+"/answers", tokenA, fmt.Sprintf(`{"commandId":"ans-%s-stale","questionSetId":"%s","questionSetRevision":"2","answer":{"transferId":"%s","digest":"%s"}}`, run, questionSetID, answerTransfer, answerDigest))
	require.Equal(t, 409, code, "%v", stale)
	require.Equal(t, "REVISION_CONFLICT", stale["error"].(map[string]any)["code"])
	code, answer := e.call("POST", "/api/v1/preparations/"+prepID+"/answers", tokenA, fmt.Sprintf(`{"commandId":"ans-%s","questionSetId":"%s","questionSetRevision":"1","answer":{"transferId":"%s","digest":"%s"}}`, run, questionSetID, answerTransfer, answerDigest))
	require.Equal(t, 202, code, "%v", answer)
	code, dup := e.call("POST", "/api/v1/preparations/"+prepID+"/answers", tokenA, fmt.Sprintf(`{"commandId":"ans-%s","questionSetId":"%s","questionSetRevision":"1","answer":{"transferId":"%s","digest":"%s"}}`, run, questionSetID, answerTransfer, answerDigest))
	require.Equal(t, 202, code, "%v", dup)
	require.Equal(t, answer["answerId"], dup["answerId"], "a duplicate answer is idempotent")

	succeeded := await(prepID, func(v map[string]any) bool { return v["lifecycle"] == "succeeded" }, 2*time.Minute)
	require.Equal(t, "brief_frozen", succeeded["phase"])
	briefID, _ := succeeded["subject"].(map[string]any)["briefId"].(string)
	require.NotEmpty(t, briefID, "the succeeded preparation names its brief")
	require.Equal(t, int32(2), up.receives.Load(), "two physical sends: one per analysis round")
	var relay string
	require.NoError(t, conn.QueryRow(ctx, "SELECT relay_state FROM answers WHERE answer_id = $1", answer["answerId"]).Scan(&relay))
	require.Equal(t, "applied", relay, "the answer's tracked Update was applied by the run")
	var briefState, briefHandle string
	require.NoError(t, conn.QueryRow(ctx, "SELECT state, handle FROM briefs WHERE brief_id = $1", briefID).Scan(&briefState, &briefHandle))
	require.Equal(t, "current", briefState)
	var briefBytes int64
	require.NoError(t, conn.QueryRow(ctx, "SELECT actual_size FROM artifact_transfers WHERE handle = $1 AND state = 'finalized' AND class = 'brief'", briefHandle).Scan(&briefBytes))
	require.Greater(t, briefBytes, int64(0), "the brief is a finalized artifact bound to the preparation")
	var closedAttempts int
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM attempts WHERE operation_id = $1 AND state = 'closed' AND outcome = 'completed'", prepID).Scan(&closedAttempts))
	require.Equal(t, 2, closedAttempts, "one analysis attempt per round, both closed completed")

	// Generation against the exact brief: admitted, funded and leased under
	// stable occurrences, then refused at the codegen launch by this
	// environment's launcher (codegen-team-dev-v1 is not enabled here; P2.4 AC4 open).
	subject := digestOf("gen-subject-" + run)
	code, gen := e.call("POST", "/api/v1/operations", tokenA, fmt.Sprintf(`{"commandId":"gen-%s","kind":"generation","subject":{"profileId":"generation-v1","subjectDigest":"%s","briefId":"%s"}}`, run, subject, briefID))
	require.Equal(t, 202, code, "%v", gen)
	genID := gen["operationId"].(string)
	final := await(genID, func(v map[string]any) bool { return v["lifecycle"] == "failed" || v["lifecycle"] == "succeeded" }, 3*time.Minute)
	require.Equal(t, "failed", final["lifecycle"])
	require.Equal(t, "PROFILE_UNQUALIFIED", final["failureCode"], "the codegen launch is refused: codegen-team-dev-v1 (state development) is not in this environment's kubernetes.enabled_profiles")
	require.NotEmpty(t, final["activeDeadline"], "the first permit set the active deadline")
	var leaseState string
	var leaseOccurrence int64
	var permits int
	var allocations int
	require.NoError(t, conn.QueryRow(ctx, "SELECT lease_state, lease_occurrence FROM operations WHERE operation_id = $1", genID).Scan(&leaseState, &leaseOccurrence))
	require.Equal(t, "released", leaseState, "the lease acquired at bootstrap was released with the operation")
	require.GreaterOrEqual(t, leaseOccurrence, int64(2))
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM permits WHERE owner_id = $1 AND state = 'active'", genID).Scan(&permits))
	require.Equal(t, 0, permits, "the execution permit is released by the settlement")
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM allocations WHERE operation_id = $1", genID).Scan(&allocations))
	require.Equal(t, 3, allocations, "funded once from the three pool levels")
	var launches int
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM launches WHERE operation_id = $1", genID).Scan(&launches))
	require.Equal(t, 1, launches, "the launch obligation exists; the create was refused before any Kubernetes object")
	raw, err := json.Marshal(final)
	require.NoError(t, err)
	require.NotContains(t, strings.ToLower(string(raw)), "uploadcapability")

	// P1.6 AC2: a Workflow that states its outcome while an obligation is
	// open, and whose run then ends without settling (its history limit,
	// simulated by terminating it), still leaves the operation ending as it
	// intended: Control re-applies the stored settlement intent once the
	// last obligation resolves. The obligation is an unknown financial
	// exposure injected while the analysis send is held at the upstream.
	up.next(upstreamBehavior{kind: "stream-then-hold", text: []string{"thinking"}})
	stoppedTransfer, stoppedDigest := finalizedArtifact("prompt-stopped", "prompt", "text/plain", []byte("A pricing table, run "+run+"\n"))
	code, stopped := e.call("POST", "/api/v1/preparations", tokenA, fmt.Sprintf(`{"commandId":"prep-stopped-%s","prompt":{"transferId":"%s","digest":"%s"},"brandReferences":[{"sourceId":"brand_%s_stopped","revision":"1"}]}`, run, stoppedTransfer, stoppedDigest, run))
	require.Equal(t, 202, code, "%v", stopped)
	stoppedID := stopped["operationId"].(string)
	up.awaitHeld(t)
	_, err = conn.Exec(ctx, "UPDATE operations SET finance_state = 'exposure_unknown' WHERE operation_id = $1", stoppedID)
	require.NoError(t, err)
	view := await(stoppedID, func(map[string]any) bool { return true }, time.Minute)
	code, cancelled := e.call("POST", "/api/v1/operations/"+stoppedID+"/commands", tokenA, fmt.Sprintf(`{"commandId":"cancel-stopped-%s","kind":"cancel","expectedRevision":"%s"}`, run, view["revision"]))
	require.Equal(t, 202, code, "%v", cancelled)
	require.Eventually(t, func() bool {
		var intent string
		if err := conn.QueryRow(ctx, "SELECT outcome FROM operation_settlements WHERE operation_id = $1", stoppedID).Scan(&intent); err != nil {
			return false
		}
		return intent == "canceled"
	}, 2*time.Minute, 300*time.Millisecond, "the run stated its outcome; Control stored the intent")
	var lifecycle string
	require.NoError(t, conn.QueryRow(ctx, "SELECT lifecycle FROM operations WHERE operation_id = $1", stoppedID).Scan(&lifecycle))
	require.Equal(t, "reconciling", lifecycle, "the open obligation keeps the settlement reconciling")
	tc, err := client.Dial(client.Options{HostPort: e.temporal, Namespace: "anvilkit"})
	require.NoError(t, err)
	defer tc.Close()
	require.NoError(t, tc.TerminateWorkflow(ctx, stoppedID, "", "P1.6 AC2: history limit simulated"))
	desc, err := tc.DescribeWorkflowExecution(ctx, stoppedID, "")
	require.NoError(t, err)
	require.Equal(t, enumspb.WORKFLOW_EXECUTION_STATUS_TERMINATED, desc.GetWorkflowExecutionInfo().GetStatus(), "no Workflow polls the settlement any more")
	// The obligations resolve: the held send ends and the exposure is reconciled.
	up.release()
	_, err = conn.Exec(ctx, "UPDATE operations SET finance_state = 'settled' WHERE operation_id = $1", stoppedID)
	require.NoError(t, err)
	final = await(stoppedID, func(v map[string]any) bool { return v["lifecycle"] != "reconciling" }, 2*time.Minute)
	require.Equal(t, "canceled", final["lifecycle"], "settled by Control from the stored intent")
	var terminal int
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM operation_events WHERE operation_id = $1 AND transition_id LIKE 'settle:%:terminal'", stoppedID).Scan(&terminal))
	require.Equal(t, 1, terminal)
	require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM permits WHERE owner_id = $1 AND state = 'active'", stoppedID).Scan(&permits))
	require.Zero(t, permits)
	t.Logf("P1.6 AC2: operation %s settled %s by Control after its Workflow run was terminated", stoppedID, final["lifecycle"])
	_ = doubles
}
