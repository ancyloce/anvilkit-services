//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"github.com/tmaxmax/go-sse"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	"github.com/ancyloce/anvilkit-agent-contracts/go/jobschema"
	"github.com/ancyloce/anvilkit-agent-contracts/go/modelproxyapi"
)

// modelProxyControlConfig is the P06 dispatch configuration plus the
// reviewed fixture price and authorization of the Proxy's controlled route
// (provider fixture, model fixture-model: what the Proxy's configuration
// binds) and the recovery operator. No real price or provider is involved.
func modelProxyControlConfig(t *testing.T, root, dir string) string {
	t.Helper()
	base, err := os.ReadFile(recoveryControlConfig(t, root, dir))
	require.NoError(t, err)
	text := string(base)
	const route = `      - revision: fx-int-proxy
        kind: model
        route: controlled-openai-v1
        provider: fixture
        model: fixture-model
        currency: USD
        effective_from: 2026-01-01T00:00:00Z
        effective_until: 2027-01-01T00:00:00Z
        per_million: {input_units: "3000000", output_units: "15000000", reasoning_units: "15000000", cached_input_units: "300000"}
        max_exposure: "1000000"
      - revision: fx-int-tool`
	require.Contains(t, text, "      - revision: fx-int-tool")
	text = strings.Replace(text, "      - revision: fx-int-tool", route, 1)
	text = strings.Replace(text, "        route_id: fixture-route", "        route_id: fixture-route\n      - tenant_id: tenant_a\n        route_id: controlled-openai-v1", 1)
	require.Contains(t, text, "route_id: controlled-openai-v1")
	path := filepath.Join(dir, "control.model-proxy.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

// modelUpstream is the countable controlled upstream: an OpenAI-compatible
// chat-completions double that records every physical receive and answers
// as scripted. It stands in for a provider; nothing here is a paid-provider
// claim.
type modelUpstream struct {
	srv      *httptest.Server
	receives atomic.Int32
	mu       sync.Mutex
	script   []upstreamBehavior
	auths    []string
	bodies   []string
	hanging  []chan struct{}
}

type upstreamBehavior struct {
	kind   string // stream, status, hang, stream-then-hang, stream-then-hold
	text   []string
	usage  *bool // nil: default usage; false: none; true: zero usage
	status int
	// tool, when set, emits one streamed tool call (id, name, arguments JSON).
	tool []string
	// usageRaw, when set, is the usage member of the final chunk as written (any shape; usage is ignored).
	usageRaw map[string]any
}

func newModelUpstream(t *testing.T) *modelUpstream {
	u := &modelUpstream{}
	u.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		u.receives.Add(1)
		u.mu.Lock()
		u.auths = append(u.auths, r.Header.Get("Authorization"))
		u.bodies = append(u.bodies, string(body))
		var b upstreamBehavior
		if len(u.script) > 0 {
			b, u.script = u.script[0], u.script[1:]
		} else {
			b = upstreamBehavior{kind: "stream"}
		}
		u.mu.Unlock()
		// One chat.completion.chunk per SSE event, encoded by go-sse.
		emit := func(c map[string]any) {
			c["id"], c["object"], c["created"], c["model"] = "chatcmpl-integration", "chat.completion.chunk", 1, "fixture-model"
			raw, _ := json.Marshal(c)
			m := &sse.Message{}
			m.AppendData(string(raw))
			_, _ = m.WriteTo(w)
			w.(http.Flusher).Flush()
		}
		done := func() {
			m := &sse.Message{}
			m.AppendData("[DONE]")
			_, _ = m.WriteTo(w)
		}
		chunk := func(delta string, finish *string, usage map[string]any) {
			if usage != nil {
				emit(map[string]any{"choices": []any{}, "usage": usage})
				return
			}
			ch := map[string]any{"index": 0, "delta": map[string]any{"content": delta}, "finish_reason": nil}
			if finish != nil {
				ch["finish_reason"] = *finish
			}
			emit(map[string]any{"choices": []any{ch}})
		}
		switch b.kind {
		case "status":
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(b.status)
			_, _ = w.Write([]byte(`{"error":{"message":"scripted failure"}}`))
			return
		case "hang", "stream-then-hang", "stream-then-hold":
			w.Header().Set("Content-Type", "text/event-stream")
			w.WriteHeader(200)
			w.(http.Flusher).Flush()
			for _, t := range b.text {
				chunk(t, nil, nil)
			}
			release := make(chan struct{})
			u.mu.Lock()
			u.hanging = append(u.hanging, release)
			u.mu.Unlock()
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			if b.kind != "stream-then-hold" {
				return // the stream ends without its usage: the outcome is unknown
			}
			stop := "stop"
			chunk("", &stop, nil)
			chunk("", nil, map[string]any{"prompt_tokens": 100, "completion_tokens": 50})
			done()
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(200)
		text := b.text
		if text == nil {
			text = []string{"Hello", " from", " the controlled upstream"}
		}
		for _, t := range text {
			chunk(t, nil, nil)
		}
		stop := "stop"
		if len(b.tool) == 3 {
			emit(map[string]any{"choices": []any{map[string]any{"index": 0, "delta": map[string]any{"tool_calls": []any{map[string]any{"index": 0, "id": b.tool[0], "type": "function", "function": map[string]any{"name": b.tool[1], "arguments": ""}}}}, "finish_reason": nil}}})
			emit(map[string]any{"choices": []any{map[string]any{"index": 0, "delta": map[string]any{"tool_calls": []any{map[string]any{"index": 0, "function": map[string]any{"arguments": b.tool[2]}}}}, "finish_reason": nil}}})
			stop = "tool_calls"
		}
		chunk("", &stop, nil)
		switch {
		case b.usageRaw != nil:
			chunk("", nil, b.usageRaw)
		case b.usage == nil:
			chunk("", nil, map[string]any{"prompt_tokens": 100, "completion_tokens": 50, "prompt_tokens_details": map[string]any{"cached_tokens": 0}, "completion_tokens_details": map[string]any{"reasoning_tokens": 0}})
		case *b.usage:
			chunk("", nil, map[string]any{"prompt_tokens": 0, "completion_tokens": 0})
		}
		done()
	}))
	t.Cleanup(u.srv.Close)
	return u
}

func (u *modelUpstream) next(b ...upstreamBehavior) {
	u.mu.Lock()
	defer u.mu.Unlock()
	u.script = append(u.script, b...)
}

// pending is the number of scripted answers no receive has consumed: a
// second wire send beneath one admission would have taken one.
func (u *modelUpstream) pending() int {
	u.mu.Lock()
	defer u.mu.Unlock()
	return len(u.script)
}

func (u *modelUpstream) release() {
	u.mu.Lock()
	defer u.mu.Unlock()
	for _, c := range u.hanging {
		close(c)
	}
	u.hanging = nil
}

// awaitHeld waits until the upstream holds an answer open: the observable
// fact that the Proxy's physical send is in flight (a record or a first
// frame precedes it by a few milliseconds and would let a release run early).
func (u *modelUpstream) awaitHeld(t *testing.T) {
	t.Helper()
	require.Eventually(t, func() bool {
		u.mu.Lock()
		defer u.mu.Unlock()
		return len(u.hanging) > 0
	}, 20*time.Second, 50*time.Millisecond, "the upstream holds the send")
}

// buildModelProxy builds the Proxy from its repository alone (its locked
// install and build; the contracts it validates against are the parent's
// checkout, named through the environment as the image names its context).
func buildModelProxy(t *testing.T, root string) string {
	t.Helper()
	dir := filepath.Join(root, "services", "agent", "model-proxy")
	for _, args := range [][]string{{"pnpm", "install", "--frozen-lockfile", "--ignore-scripts"}, {"pnpm", "run", "build"}} {
		cmd := exec.Command(args[0], args[1:]...)
		cmd.Dir = dir
		out, err := cmd.CombinedOutput()
		require.NoError(t, err, string(out))
	}
	return filepath.Join(dir, "dist", "main.js")
}

const (
	proxyTokenWorkflow = "integration-workflow-token"
	proxyTokenSidecar  = "integration-sidecar-token"
	proxyTokenControl  = "integration-control-token"
	proxyCredential    = "integration-fixture-credential"
)

// proxyProcess is one Model Proxy process of the scenario.
type proxyProcess struct {
	name, url, storeDir, cfg, principals, dist, controlAddr, root string
	port                                                          string
	// healthURL is the process's probe listener (plaintext, /healthz and /readyz only).
	healthURL string
	cmd       *exec.Cmd
	log       *os.File
	t         *testing.T
}

func writeProxyConfig(t *testing.T, root, dir, listen, upstreamURL string) string {
	t.Helper()
	base, err := os.ReadFile(filepath.Join(root, "services", "agent", "model-proxy", "config.yaml"))
	require.NoError(t, err)
	text := string(base)
	for _, r := range [][2]string{
		{"mode: disabled", "mode: development"},
		{"    enabled: false\n", "    enabled: true\n"},
		{"base_url: http://127.0.0.1:1/v1", "base_url: " + upstreamURL + "/v1"},
		{"listen: 127.0.0.1:9103", "listen: " + listen},
		{"  retry_initial: 1s\n  retry_max_interval: 30s\n  sweep_interval: 30s\n  reclaim_grace: 30s", "  retry_initial: 200ms\n  retry_max_interval: 2s\n  sweep_interval: 1s\n  reclaim_grace: 1s"},
		{"    initial: 500ms\n    max_interval: 5s\n    max_attempts: 5", "    initial: 200ms\n    max_interval: 1s\n    max_attempts: 3"},
		{"  sse_heartbeat: 15s", "  sse_heartbeat: 2s"},
	} {
		require.Contains(t, text, r[0])
		text = strings.Replace(text, r[0], r[1], 1)
	}
	path := filepath.Join(dir, "model-proxy.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

func (p *proxyProcess) start() {
	p.t.Helper()
	logf, err := os.OpenFile(filepath.Join(filepath.Dir(p.cfg), p.name+".log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	require.NoError(p.t, err)
	cmd := exec.Command("node", p.dist)
	cmd.Dir = filepath.Join(p.root, "services", "agent", "model-proxy")
	cmd.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + os.Getenv("HOME"),
		"ANVILKIT_MODEL_PROXY_CONFIG=" + p.cfg, "ANVILKIT_MODEL_PROXY_CONTROL_ADDRESS=" + p.controlAddr, "ANVILKIT_MODEL_PROXY_PRINCIPALS_FILE=" + p.principals,
		"ANVILKIT_MODEL_PROXY_HEALTH_LISTEN=" + strings.TrimPrefix(p.healthURL, "http://"),
		"ANVILKIT_MODEL_PROXY_STORE_DIR=" + p.storeDir, "ANVILKIT_MODEL_PROXY_CONTRACTS_DIR=" + filepath.Join(p.root, "contracts"),
		"ANVILKIT_MODEL_PROXY_CREDENTIAL_CONTROLLED_OPENAI_V1=" + proxyCredential}
	cmd.Env = append(cmd.Env, identityEnv(p.t, "ANVILKIT_MODEL_PROXY_CONTROL_", principalModelProxy)...)
	cmd.Stdout, cmd.Stderr = logf, logf
	require.NoError(p.t, cmd.Start(), p.name)
	p.cmd, p.log = cmd, logf
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if resp, err := http.Get(p.healthURL + "/readyz"); err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 {
				return
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	raw, _ := os.ReadFile(logf.Name())
	p.t.Fatalf("%s did not become ready:\n%s", p.name, raw)
}

func (p *proxyProcess) kill() {
	if p.cmd != nil && p.cmd.Process != nil {
		_ = p.cmd.Process.Kill()
		_ = p.cmd.Wait()
		p.log.Close()
		p.cmd = nil
	}
}

func (p *proxyProcess) stop() {
	if p.cmd != nil && p.cmd.Process != nil {
		_ = p.cmd.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { _ = p.cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(15 * time.Second):
			_ = p.cmd.Process.Kill()
		}
		p.log.Close()
		p.cmd = nil
	}
}

// modelCall is one caller's POST of the frozen transport: the SSE answer is
// collected into frames; stopAfter ends the read early (a disconnect).
type modelCallReply struct {
	status int
	frames []modelproxyapi.StreamFrame
	body   []byte
}

func postModelCall(t *testing.T, url, token string, req modelproxyapi.ModelCallRequest, stopAfter int) modelCallReply {
	t.Helper()
	raw, err := json.Marshal(req)
	require.NoError(t, err)
	httpReq, err := http.NewRequest("POST", url+"/api/v1/model-calls", strings.NewReader(string(raw)))
	require.NoError(t, err)
	httpReq.Header.Set("Authorization", "Bearer "+token)
	httpReq.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 3 * time.Minute}
	resp, err := client.Do(httpReq)
	require.NoError(t, err)
	defer resp.Body.Close()
	reply := modelCallReply{status: resp.StatusCode}
	if !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		reply.body, _ = io.ReadAll(resp.Body)
		return reply
	}
	// go-sse reads the event stream as the protocol states it; every frame decodes strictly as the contract.
	for ev, err := range sse.Read(resp.Body, &sse.ReadConfig{MaxEventSize: 1 << 20}) {
		if err != nil || ev.Data == "" {
			continue
		}
		var f modelproxyapi.StreamFrame
		dec := json.NewDecoder(strings.NewReader(ev.Data))
		dec.DisallowUnknownFields()
		require.NoError(t, dec.Decode(&f), "every frame decodes strictly as the contract: %s", ev.Data)
		reply.frames = append(reply.frames, f)
		if stopAfter > 0 && len(reply.frames) >= stopAfter {
			return reply
		}
		if f.Type == modelproxyapi.StreamFrameTypeDone || f.Type == modelproxyapi.StreamFrameTypeError {
			return reply
		}
	}
	return reply
}

func getModelCall(t *testing.T, url, token, callID string) (int, modelproxyapi.ModelCall) {
	t.Helper()
	req, err := http.NewRequest("GET", url+"/api/v1/model-calls/"+callID, nil)
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	var m modelproxyapi.ModelCall
	if resp.StatusCode == 200 {
		dec := json.NewDecoder(resp.Body)
		dec.DisallowUnknownFields()
		require.NoError(t, dec.Decode(&m))
	}
	return resp.StatusCode, m
}

func cancelModelCall(t *testing.T, url, token, callID string) int {
	t.Helper()
	req, err := http.NewRequest("POST", url+"/api/v1/model-calls/"+callID+"/cancellations", strings.NewReader("{}"))
	require.NoError(t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	resp.Body.Close()
	return resp.StatusCode
}

func modelRequest(callID, opID, attemptID, instanceID string, deadline time.Time) modelproxyapi.ModelCallRequest {
	req := modelproxyapi.ModelCallRequest{
		CallId:  callID,
		Binding: modelproxyapi.ExecutionBinding{TenantId: "tenant_a", OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
		RouteId: "controlled-openai-v1", RequestDigest: digestOf("request " + callID),
		Messages:        []modelproxyapi.Message{{Role: "system", Content: "You are the planner."}, {Role: "user", Content: "Plan the hero component."}},
		MaxOutputTokens: 256, MaxExposure: modelproxyapi.Money{Currency: "USD", Amount: "2000"},
		Deadline: deadline.UTC().Truncate(time.Millisecond).Format("2006-01-02T15:04:05.000Z"),
	}
	if instanceID != "" {
		req.Binding.InstanceId = &instanceID
	}
	return req
}

func frameTypes(frames []modelproxyapi.StreamFrame) []string {
	out := make([]string, 0, len(frames))
	for _, f := range frames {
		out = append(out, string(f.Type))
	}
	return out
}

// TestModelProxyControlledCalls runs P11 against two real Control
// processes, two Model Proxy processes sharing one record store, the
// Workflow's CallModel Activity (executed by the Workflow repository's own
// integration test against these processes), the access sidecar as UID
// 10002 and a countable controlled upstream. Every claim about sends is
// the upstream's receive count; every claim about the ledger is Control's
// database; every claim about evidence is the Proxy's store.
func TestModelProxyControlledCalls(t *testing.T) {
	isolatedControlDatabase(t)
	root := repoRoot(t)
	scratch := t.TempDir()
	portA, portB := freePort(t), freePort(t)
	proxyURLA, proxyURLB := "http://127.0.0.1:"+portA, "http://127.0.0.1:"+portB
	healthURLA, healthURLB := "http://127.0.0.1:"+freePort(t), "http://127.0.0.1:"+freePort(t)
	// No Workflow worker: as in the P06 scenarios the attempts stay open for
	// the senders (a LocalCheck run would close them within seconds); the
	// recovery reconciliation is driven through Control's RecoveryService,
	// the same steps the RecoveryWorkflow's Activities call.
	e := setupProcessesEnv(t, true, "", modelProxyControlConfig(t, root, scratch), true,
		[]string{"ANVILKIT_CONTROL_MODEL_PROXY_ADDRESS=" + proxyURLA, "ANVILKIT_CONTROL_MODEL_PROXY_TOKEN=" + proxyTokenControl})
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	up := newModelUpstream(t)
	dist := buildModelProxy(t, root)
	principals := filepath.Join(scratch, "model-proxy-principals.json")
	require.NoError(t, os.WriteFile(principals, []byte(fmt.Sprintf(`{%q:{"principalId":"anvilkit-agent-workflow","kind":"workflow"},%q:{"principalId":"anvilkit-job-access-sidecar","kind":"sidecar"},%q:{"principalId":"anvilkit-agent-control","kind":"control"}}`, proxyTokenWorkflow, proxyTokenSidecar, proxyTokenControl)), 0o600))
	storeDir := filepath.Join(scratch, "store")
	require.NoError(t, os.MkdirAll(filepath.Join(scratch, "a"), 0o700))
	require.NoError(t, os.MkdirAll(filepath.Join(scratch, "b"), 0o700))
	proxyA := &proxyProcess{name: "proxy-a", url: proxyURLA, healthURL: healthURLA, port: portA, storeDir: storeDir, cfg: writeProxyConfig(t, root, filepath.Join(scratch, "a"), "127.0.0.1:"+portA, up.srv.URL), principals: principals, dist: dist, controlAddr: e.controlAddr, root: root, t: t}
	proxyB := &proxyProcess{name: "proxy-b", url: proxyURLB, healthURL: healthURLB, port: portB, storeDir: storeDir, cfg: writeProxyConfig(t, root, filepath.Join(scratch, "b"), "127.0.0.1:"+portB, up.srv.URL), principals: principals, dist: dist, controlAddr: e.controlAddrB, root: root, t: t}
	proxyA.start()
	proxyB.start()
	t.Cleanup(func() {
		proxyA.stop()
		proxyB.stop()
		if t.Failed() {
			t.Logf("proxy-a log:\n%s\nproxy-b log:\n%s", logTail(t, filepath.Join(scratch, "a", "proxy-a.log"), 40), logTail(t, filepath.Join(scratch, "b", "proxy-b.log"), 40))
		}
	})

	opsA, execA := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	dispA := controlv1.NewDispatchServiceClient(e.dialControl(e.controlAddr, principalModelProxy))
	conn := e.db()
	for _, row := range []struct {
		level         string
		tenant, actor *string
		cap           int64
	}{{"platform", nil, nil, 1_000_000_000}, {"tenant", strPtr("tenant_a"), nil, 100_000_000}, {"actor", strPtr("tenant_a"), strPtr("user_a"), 100_000_000}} {
		_, err := conn.Exec(ctx, "INSERT INTO budget_pools (pool_id, level, tenant_id, actor_id, period_start, period_end, currency, cap_amount) VALUES ($1, $2, $3, $4, now() - interval '1 hour', now() + interval '1 hour', 'USD', $5)",
			"pool-"+run+"-"+row.level, row.level, row.tenant, row.actor, row.cap)
		require.NoError(t, err)
	}
	t.Cleanup(func() {
		_, _ = conn.Exec(context.Background(), "UPDATE budget_pools SET period_end = period_start + interval '1 second' WHERE pool_id LIKE $1", "pool-"+run+"-%")
	})
	dispatchOf := func(callID string) (state string, reserved int64, observations int, actual int64) {
		t.Helper()
		var id string
		require.NoError(t, conn.QueryRow(ctx, "SELECT dispatch_id, state, reserved_amount FROM dispatches WHERE call_id = $1 AND owner = 'anvilkit-agent-model-proxy'", callID).Scan(&id, &state, &reserved))
		require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM usage_observations WHERE dispatch_id = $1", id).Scan(&observations))
		require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(sum(amount), 0) FROM cost_entries WHERE dispatch_id = $1 AND kind IN ('actual', 'correction')", id).Scan(&actual))
		return
	}
	reservedOf := func(opID string) int64 {
		var v int64
		require.NoError(t, conn.QueryRow(ctx, "SELECT coalesce(max(reserved), 0) FROM allocations WHERE operation_id = $1", opID).Scan(&v))
		return v
	}
	evidenceOf := func(callID string) map[string]any {
		raw, err := os.ReadFile(filepath.Join(storeDir, "evidence", callID, "tenant_a"))
		require.NoError(t, err, "native evidence is persisted")
		var ev map[string]any
		require.NoError(t, json.Unmarshal(raw, &ev))
		return ev
	}
	opID, attemptID := e.fundedOperation(ctx, t, opsA, execA, run, "proxy", 50_000_000)
	deadline := time.Now().Add(5 * time.Minute)

	t.Run("a Go Activity completes the controlled chain: one physical send, a reviewed tool by its digest, native usage observed, a retry reenters", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "stream", tool: []string{"tc_1", "read_brief", `{"section":"hero"}`}})
		before := up.receives.Load()
		callID := "call-" + run + "-activity"
		// The reviewed schema of read_brief as the Proxy's configuration
		// states it; Go reproduces the Proxy's canonical digest (members
		// sorted, no whitespace) so the caller names the tool by it.
		schemaDigest := canonicalDigest(t, `{"type":"object","additionalProperties":false,"properties":{"section":{"type":"string","maxLength":64}}}`)
		input := map[string]any{"CallID": callID, "TenantID": "tenant_a", "OperationID": opID, "AttemptID": attemptID, "ExecutionEpoch": 1, "RouteID": "controlled-openai-v1",
			"Messages":        []map[string]string{{"Role": "system", "Content": "You are the planner."}, {"Role": "user", "Content": "Plan the hero component."}},
			"Tools":           []map[string]string{{"Name": "read_brief", "Description": "Reads the frozen brief.", "InputSchemaDigest": schemaDigest}},
			"MaxOutputTokens": 256, "MaxExposure": map[string]string{"Currency": "USD", "Amount": "2000"}, "Deadline": deadline.UTC().Format(time.RFC3339Nano)}
		raw, _ := json.Marshal(input)
		result := filepath.Join(scratch, "activity-result.json")
		cmd := exec.Command("go", "test", "-tags", "integration", "-count=1", "-run", "TestCallModelActivityChain", "./internal/adapters/modelproxy/")
		cmd.Dir = filepath.Join(root, "services", "agent", "workflow")
		cmd.Env = append(os.Environ(), "ANVILKIT_INTEGRATION_MODEL_PROXY_URL="+proxyURLA, "ANVILKIT_INTEGRATION_MODEL_PROXY_TOKEN="+proxyTokenWorkflow,
			"ANVILKIT_INTEGRATION_MODEL_PROXY_INPUT="+string(raw), "ANVILKIT_INTEGRATION_MODEL_PROXY_RESULT="+result)
		out, err := cmd.CombinedOutput()
		require.NoError(t, err, string(out))
		require.Contains(t, string(out), "ok ", string(out))
		require.NotContains(t, string(out), "SKIP")
		resRaw, err := os.ReadFile(result)
		require.NoError(t, err)
		var results []struct {
			State, Text, DispatchID, NativeReference string
			Frames                                   int
			Usage                                    *struct{ InputUnits, OutputUnits string }
			ToolCalls                                []struct{ ToolCallID, Name, Arguments, ArgumentsDigest string }
		}
		require.NoError(t, json.Unmarshal(resRaw, &results))
		require.Len(t, results, 4, "the call, its reentry, the query and the tool round trip's next call")
		require.Equal(t, "succeeded", results[0].State)
		require.Equal(t, "Hello from the controlled upstream", results[0].Text)
		require.Len(t, results[0].ToolCalls, 1, "the reviewed tool, named by the digest Go computed, reached the upstream and its call came back")
		require.Equal(t, "read_brief", results[0].ToolCalls[0].Name)
		require.Equal(t, `{"section":"hero"}`, results[0].ToolCalls[0].Arguments)
		require.Equal(t, digestOf(`{"section":"hero"}`), results[0].ToolCalls[0].ArgumentsDigest)
		require.Contains(t, up.bodies[len(up.bodies)-2], `"name":"read_brief"`, "the Proxy sent the reviewed schema, not the caller's")
		require.Equal(t, "chatcmpl-integration", results[0].NativeReference)
		require.Equal(t, "100", results[0].Usage.InputUnits)
		require.Equal(t, before+2, up.receives.Load(), "two Activity executions and a query: one physical send; the tool round trip's next call: one more")
		require.Equal(t, "Bearer "+proxyCredential, up.auths[len(up.auths)-1], "the upstream saw the route credential only")
		require.NotContains(t, up.bodies[len(up.bodies)-1], proxyTokenWorkflow)
		state, reserved, observations, actual := dispatchOf(callID)
		require.Equal(t, "observed", state)
		require.Equal(t, 1, observations)
		require.Equal(t, int64(100*3+50*15), actual, "native usage priced by Control's fixture, not by the Proxy")
		_ = reserved
		require.Zero(t, reservedOf(opID), "a definite outcome with usage releases the reservation")
		ev := evidenceOf(callID)
		require.Contains(t, ev["request"].(map[string]any)["body"], "Plan the hero component.")
		require.NotContains(t, ev["request"].(map[string]any)["body"], proxyCredential)
		require.EqualValues(t, 200, ev["response"].(map[string]any)["status"])
		// The tool round trip: the Activity executed the tool and the next call —
		// its own call id, its own admission — replayed the assistant tool call
		// and the matching tool result to the upstream as the protocol states.
		require.Equal(t, "succeeded", results[3].State)
		nextState, _, nextObservations, _ := dispatchOf(callID + "-2")
		require.Equal(t, "observed", nextState)
		require.Equal(t, 1, nextObservations)
		var replayed struct {
			Messages []map[string]any `json:"messages"`
		}
		require.NoError(t, json.Unmarshal([]byte(up.bodies[len(up.bodies)-1]), &replayed))
		require.Len(t, replayed.Messages, 4)
		require.Equal(t, "assistant", replayed.Messages[2]["role"])
		toolCalls, _ := json.Marshal(replayed.Messages[2]["tool_calls"])
		require.JSONEq(t, `[{"id":"tc_1","type":"function","function":{"name":"read_brief","arguments":"{\"section\":\"hero\"}"}}]`, string(toolCalls))
		require.Equal(t, map[string]any{"role": "tool", "tool_call_id": "tc_1", "content": `tool read_brief executed by the caller: {"section":"hero"}`}, replayed.Messages[3])
	})

	t.Run("the access sidecar relays under the confirmed scope: the binding is the registered instance, not the caller's word", func(t *testing.T) {
		if os.Geteuid() != 0 {
			skipMissing(t, "root", "the sidecar runs as UID 10002 and needs a root caller")
		}
		bin, err := os.MkdirTemp("/tmp", "anvilkit-sidecar-")
		require.NoError(t, err)
		t.Cleanup(func() { os.RemoveAll(bin) })
		require.NoError(t, os.Chmod(bin, 0o755))
		sidecar := filepath.Join(bin, "anvilkit-job-access-sidecar")
		build := exec.Command("go", "build", "-o", sidecar, "./cmd/anvilkit-job-access-sidecar")
		build.Dir = filepath.Join(root, "jobs", "shared", "access-sidecar")
		out, err := build.CombinedOutput()
		require.NoError(t, err, string(out))
		profile, err := jobschema.ProfileByID("harness-wiring-dev-v1")
		require.NoError(t, err)
		sideOp, sideAttempt := e.fundedOperation(ctx, t, opsA, execA, run, "sidecar", 5_000_000)
		// fundedOperation opens the attempt as the LocalCheck step; the harness
		// envelope names the attempt as it is registered.
		launchKey := "mp-" + strings.ToLower(run[len(run)-8:])
		attemptDeadline := time.Now().Add(4 * time.Minute)
		launch, err := execA.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run + "-sidecar", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
			AttemptId: sideAttempt, LaunchKey: launchKey, Backend: "host-process", ImageDigest: profile.Image.Digest, Deadline: timestamppb.New(attemptDeadline),
		})
		require.NoError(t, err)
		reg, err := execA.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "reg-" + run + "-sidecar", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("reg")},
			AttemptId: sideAttempt, LaunchKey: launchKey, Backend: "host-process", JobUid: "job-" + run, PodUid: "pod-" + run, ImageDigest: profile.Image.Digest,
		})
		require.NoError(t, err)
		require.True(t, reg.GetInstance().GetCurrent())
		attempt, err := execA.GetInstance(ctx, &controlv1.GetInstanceRequest{Backend: "host-process", LaunchKey: launchKey, PodUid: "pod-" + run})
		require.NoError(t, err)
		envelope := fmt.Sprintf(`{"schemaVersion":1,"launchId":%q,"launchKey":%q,"operationId":%q,"attemptId":%q,"profileId":"local-check-v1","profileRevision":%q,"jobKind":"codegen","executionEpoch":%q,"launchEpoch":%q,"deadline":%q,"inputs":[]}`,
			launch.GetLaunchId(), launchKey, sideOp, sideAttempt, profile.Revision, attempt.GetAttempt().GetExecutionEpoch(), launch.GetLaunchEpoch(), attemptDeadline.UTC().Format(time.RFC3339))
		dir := filepath.Join(bin, "run")
		require.NoError(t, os.Mkdir(dir, 0o777))
		require.NoError(t, os.Chmod(dir, 0o777))
		cfg := filepath.Join(bin, "sidecar.yaml")
		require.NoError(t, os.WriteFile(cfg, sidecarConfig(t, dir), 0o644))
		logf, err := os.Create(filepath.Join(bin, "sidecar.log"))
		require.NoError(t, err)
		cmd := exec.Command(sidecar)
		cmd.Env = []string{"ANVILKIT_SIDECAR_CONFIG=" + cfg, "ANVILKIT_SIDECAR_CONTROL_ADDRESS=" + e.controlAddr, "ANVILKIT_SIDECAR_SOCKETS_DIR=" + filepath.Join(dir, "sockets"),
			"ANVILKIT_SIDECAR_BACKEND=host-process", "ANVILKIT_SIDECAR_LAUNCH_KEY=" + launchKey, "ANVILKIT_SIDECAR_POD_UID=pod-" + run, "ANVILKIT_SIDECAR_LAUNCH_ENVELOPE=" + envelope,
			"ANVILKIT_SIDECAR_MODEL_PROXY_URL=" + proxyURLB, "ANVILKIT_SIDECAR_MODEL_PROXY_TOKEN=" + proxyTokenSidecar}
		cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: 10002, Gid: 10002, Groups: []uint32{0, 10001}}}
		stdin, err := cmd.StdinPipe()
		require.NoError(t, err)
		cmd.Stdout, cmd.Stderr = logf, logf
		require.NoError(t, cmd.Start())
		t.Cleanup(func() {
			stdin.Close()
			_ = cmd.Process.Signal(syscall.SIGTERM)
			done := make(chan struct{})
			go func() { _ = cmd.Wait(); close(done) }()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				_ = cmd.Process.Kill()
			}
			logf.Close()
			if t.Failed() {
				raw, _ := os.ReadFile(filepath.Join(bin, "sidecar.log"))
				t.Logf("sidecar log:\n%s", raw)
			}
		})
		socket := filepath.Join(dir, "sockets", "trusted.sock")
		require.Eventually(t, func() bool {
			st, err := os.Lstat(socket)
			return err == nil && st.Mode()&os.ModeSocket != 0
		}, 20*time.Second, 100*time.Millisecond)
		require.Eventually(t, func() bool {
			code, _ := trustedCall(t, socket, "GET", "/v1/scope", nil, nil)
			return code == 200
		}, 20*time.Second, 200*time.Millisecond, "the sidecar confirmed its scope")

		before := up.receives.Load()
		callID := "call-" + run + "-sidecar"
		relay := fmt.Sprintf(`{"callId":%q,"routeId":"controlled-openai-v1","messages":[{"role":"user","content":"Plan through the sidecar."}],"maxOutputTokens":128,"maxExposure":{"currency":"USD","amount":"1500"},"deadline":%q}`, callID, time.Now().Add(3*time.Minute).UTC().Format(time.RFC3339))
		code, body := relayCall(t, socket, relay)
		require.Equal(t, 200, code, string(body))
		require.Contains(t, string(body), `"type":"done"`)
		require.NotContains(t, string(body), "chat.completion.chunk", "native bodies never reach the caller")
		require.Equal(t, before+1, up.receives.Load())
		var bound modelproxyapi.ExecutionBinding
		var dispatchOp, dispatchAttempt, dispatchInstance, state string
		require.NoError(t, conn.QueryRow(ctx, "SELECT operation_id, attempt_id, coalesce(instance_id, ''), state FROM dispatches WHERE call_id = $1", callID).Scan(&dispatchOp, &dispatchAttempt, &dispatchInstance, &state))
		require.Equal(t, sideOp, dispatchOp)
		require.Equal(t, sideAttempt, dispatchAttempt)
		require.Equal(t, reg.GetInstance().GetInstanceId(), dispatchInstance, "the dispatch is bound to the registered physical instance")
		require.Equal(t, "observed", state)
		_ = bound
		// The same bytes reenter the same call: no second send.
		code, body = relayCall(t, socket, relay)
		require.Equal(t, 200, code, string(body))
		require.Equal(t, before+1, up.receives.Load())
		// A caller-supplied binding is refused before anything is forwarded.
		code, body = relayCall(t, socket, strings.Replace(relay, `"maxExposure"`, `"binding":{"tenantId":"tenant_a","operationId":"other","attemptId":"other","executionEpoch":"1"},"maxExposure"`, 1))
		require.Equal(t, 400, code, string(body))
		require.Equal(t, before+1, up.receives.Load())
		// The launch of this fake Pod ran nothing: its attempt closes with
		// definite cleanup so the recovery scenario's launch finding settles on
		// Control's own records.
		_, err = execA.CloseAttempt(ctx, &controlv1.CloseAttemptRequest{Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "close-" + run + "-sidecar", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("close-sidecar")},
			AttemptId: sideAttempt, Outcome: controlv1.AttemptOutcome_ATTEMPT_OUTCOME_CANCELED, Cleanup: controlv1.CleanupState_CLEANUP_STATE_COMPLETE})
		require.NoError(t, err)
	})

	t.Run("F02 underdeclared exposure with insufficient allocation prevents the physical send", func(t *testing.T) {
		op, at := e.fundedOperation(ctx, t, opsA, execA, run, "underdeclared", 10_000)
		before := up.receives.Load()
		request := modelRequest("call-"+run+"-underdeclared", op, at, "", deadline)
		request.MaxExposure.Amount = "1"
		result := postModelCall(t, proxyURLA, proxyTokenWorkflow, request, 0)
		require.Equal(t, 429, result.status, string(result.body))
		require.Contains(t, string(result.body), "BUDGET_EXHAUSTED")
		require.Equal(t, before, up.receives.Load(), "allocation below the reviewed full route bound must deny before transport")
	})

	t.Run("a denied admission sends nothing and is recorded", func(t *testing.T) {
		unfundedOp, unfundedAttempt := e.fundedOperation(ctx, t, opsA, execA, run, "unfunded", 0)
		before := up.receives.Load()
		callID := "call-" + run + "-denied"
		r := postModelCall(t, proxyURLA, proxyTokenWorkflow, modelRequest(callID, unfundedOp, unfundedAttempt, "", deadline), 0)
		require.Equal(t, 429, r.status, string(r.body))
		require.Contains(t, string(r.body), "BUDGET_EXHAUSTED")
		require.Equal(t, before, up.receives.Load())
		state, _, _, _ := dispatchOf(callID)
		require.Equal(t, "denied", state)
		code, m := getModelCall(t, proxyURLB, proxyTokenWorkflow, callID)
		require.Equal(t, 200, code)
		require.Equal(t, modelproxyapi.ModelCallStateDenied, m.State)
	})

	t.Run("the same call on both Proxies through both Controls is sent once", func(t *testing.T) {
		before := up.receives.Load()
		callID := "call-" + run + "-race"
		req := modelRequest(callID, opID, attemptID, "", deadline)
		var wg sync.WaitGroup
		replies := make([]modelCallReply, 2)
		for i, url := range []string{proxyURLA, proxyURLB} {
			wg.Add(1)
			go func(i int, url string) {
				defer wg.Done()
				replies[i] = postModelCall(t, url, proxyTokenWorkflow, req, 0)
			}(i, url)
		}
		wg.Wait()
		require.Equal(t, 200, replies[0].status, string(replies[0].body))
		require.Equal(t, 200, replies[1].status, string(replies[1].body))
		require.Equal(t, frameTypes(replies[0].frames), frameTypes(replies[1].frames))
		require.Equal(t, "done", frameTypes(replies[0].frames)[len(replies[0].frames)-1])
		require.Equal(t, before+1, up.receives.Load(), "one physical send across two Proxies and two Controls")
		var rows int
		require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM dispatches WHERE call_id = $1", callID).Scan(&rows))
		require.Equal(t, 1, rows)
		state, _, observations, _ := dispatchOf(callID)
		require.Equal(t, "observed", state)
		require.Equal(t, 1, observations)
	})

	t.Run("a lost first permission answer causes no send and leaves the call unknown", func(t *testing.T) {
		before := up.receives.Load()
		callID := "call-" + run + "-lost"
		req := modelRequest(callID, opID, attemptID, "", deadline)
		// The permission is consumed under the Proxy's own command identity
		// before the Proxy's request arrives: what Control sees of a first
		// answer that never reached the sender.
		dl, err := time.Parse(time.RFC3339Nano, req.Deadline)
		require.NoError(t, err)
		first, err := dispA.AdmitModel(ctx, &controlv1.AdmitModelRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "model-call:" + callID, ActorId: "anvilkit-agent-workflow", RequestDigest: req.RequestDigest},
			Binding: &controlv1.ExecutionBinding{OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"},
			CallId:  callID, Owner: "anvilkit-agent-model-proxy", RouteId: "controlled-openai-v1", Provider: "fixture", Model: "fixture-model",
			MaxExposure: &controlv1.Money{Currency: "USD", Amount: "1000000"}, Deadline: timestamppb.New(dl),
		})
		require.NoError(t, err)
		require.True(t, first.GetAdmission().GetDispatchAllowed())
		r := postModelCall(t, proxyURLA, proxyTokenWorkflow, req, 0)
		require.Equal(t, 200, r.status, string(r.body))
		require.Equal(t, []string{"admitted", "error"}, frameTypes(r.frames))
		require.Equal(t, "PERMISSION_LOST", *r.frames[1].ErrorCode)
		require.Equal(t, before, up.receives.Load())
		state, reserved, _, _ := dispatchOf(callID)
		require.Equal(t, "unknown", state)
		require.Equal(t, int64(1_000_000), reserved, "the reviewed full exposure is retained, never settled as zero")
		var finance string
		require.NoError(t, conn.QueryRow(ctx, "SELECT finance_state FROM operations WHERE operation_id = $1", opID).Scan(&finance))
		require.Equal(t, "exposure_unknown", finance)
	})

	t.Run("a disconnected caller reconnects to the recorded frames; nothing is sent again", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "stream", text: []string{"one", " two", " three"}})
		before := up.receives.Load()
		callID := "call-" + run + "-reconnect"
		req := modelRequest(callID, opID, attemptID, "", deadline)
		partial := postModelCall(t, proxyURLA, proxyTokenWorkflow, req, 2)
		require.Len(t, partial.frames, 2)
		require.Eventually(t, func() bool {
			code, m := getModelCall(t, proxyURLB, proxyTokenWorkflow, callID)
			return code == 200 && m.State == modelproxyapi.ModelCallStateSucceeded
		}, 20*time.Second, 200*time.Millisecond, "the send completes without its caller")
		replay := postModelCall(t, proxyURLB, proxyTokenWorkflow, req, 0)
		require.Equal(t, 200, replay.status)
		require.Equal(t, []string{"admitted", "text", "text", "text", "usage", "done"}, frameTypes(replay.frames))
		require.Equal(t, before+1, up.receives.Load())
		changed := req
		changed.Messages = []modelproxyapi.Message{{Role: "user", Content: "Something else."}}
		conflict := postModelCall(t, proxyURLA, proxyTokenWorkflow, changed, 0)
		require.Equal(t, 409, conflict.status)
		require.Equal(t, before+1, up.receives.Load())
	})

	t.Run("a call is read, canceled and reentered by its own caller only; Control's identity may read it; another workload's valid token grants nothing", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "stream", text: []string{"owned"}})
		before := up.receives.Load()
		callID := "call-" + run + "-owned"
		req := modelRequest(callID, opID, attemptID, "", deadline)
		r := postModelCall(t, proxyURLA, proxyTokenWorkflow, req, 0)
		require.Equal(t, 200, r.status, string(r.body))
		require.Equal(t, before+1, up.receives.Load())
		// The sidecar's identity is valid, the call id is right: nothing is answered.
		code, _ := getModelCall(t, proxyURLB, proxyTokenSidecar, callID)
		require.Equal(t, 404, code)
		require.Equal(t, 404, cancelModelCall(t, proxyURLB, proxyTokenSidecar, callID))
		replay := postModelCall(t, proxyURLB, proxyTokenSidecar, req, 0)
		require.Equal(t, 403, replay.status, string(replay.body))
		require.Contains(t, string(replay.body), "FORBIDDEN")
		// Control's trusted identity reads the record (the recovery query below relies on it) and nothing else.
		code, m := getModelCall(t, proxyURLB, proxyTokenControl, callID)
		require.Equal(t, 200, code)
		require.Equal(t, modelproxyapi.ModelCallStateSucceeded, m.State)
		require.Equal(t, 404, cancelModelCall(t, proxyURLB, proxyTokenControl, callID))
		require.Equal(t, 403, postModelCall(t, proxyURLB, proxyTokenControl, req, 0).status)
		// The caller's other replica, through the other Proxy and Control, reads and reenters.
		code, _ = getModelCall(t, proxyURLB, proxyTokenWorkflow, callID)
		require.Equal(t, 200, code)
		again := postModelCall(t, proxyURLB, proxyTokenWorkflow, req, 0)
		require.Equal(t, 200, again.status)
		require.Equal(t, frameTypes(r.frames), frameTypes(again.frames))
		require.Equal(t, before+1, up.receives.Load())
		_, _, observations, _ := dispatchOf(callID)
		require.Equal(t, 1, observations)
	})

	t.Run("upstream failures and missing usage end unknown with the exposure retained; explicit zero usage settles", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "status", status: 500})
		before := up.receives.Load()
		failed := "call-" + run + "-500"
		r := postModelCall(t, proxyURLA, proxyTokenWorkflow, modelRequest(failed, opID, attemptID, "", deadline), 0)
		require.Equal(t, "error", frameTypes(r.frames)[len(r.frames)-1])
		require.Equal(t, "UPSTREAM_STATUS_500", *r.frames[len(r.frames)-1].ErrorCode)
		require.Equal(t, before+1, up.receives.Load(), "a retryable status reaches the upstream once")
		state, reserved, _, _ := dispatchOf(failed)
		require.Equal(t, "unknown", state)
		require.Equal(t, int64(1_000_000), reserved)
		no := false
		up.next(upstreamBehavior{kind: "stream", text: []string{"no usage"}, usage: &no})
		missing := "call-" + run + "-missing-usage"
		r = postModelCall(t, proxyURLB, proxyTokenWorkflow, modelRequest(missing, opID, attemptID, "", deadline), 0)
		require.Equal(t, "USAGE_MISSING", *r.frames[len(r.frames)-1].ErrorCode)
		state, reserved, _, _ = dispatchOf(missing)
		require.Equal(t, "unknown", state)
		require.Equal(t, int64(1_000_000), reserved)
		// Usage outside the protocol is insufficient evidence on the real ledger:
		// nothing is charged, the exposure stays reserved, the dispatch is unknown.
		for name, raw := range map[string]map[string]any{
			"empty":    {},
			"negative": {"prompt_tokens": -5, "completion_tokens": 2},
			"partial":  {"prompt_tokens": 5},
			"string":   {"prompt_tokens": "5", "completion_tokens": "2"},
		} {
			up.next(upstreamBehavior{kind: "stream", text: []string{"counted?"}, usageRaw: raw})
			invalid := "call-" + run + "-usage-" + name
			r = postModelCall(t, proxyURLB, proxyTokenWorkflow, modelRequest(invalid, opID, attemptID, "", deadline), 0)
			require.Equal(t, "error", frameTypes(r.frames)[len(r.frames)-1], name)
			require.Equal(t, "USAGE_INVALID", *r.frames[len(r.frames)-1].ErrorCode, name)
			require.Nil(t, r.frames[len(r.frames)-1].Usage, name)
			state, reserved, observations, actual := dispatchOf(invalid)
			require.Equal(t, "unknown", state, name)
			require.Equal(t, int64(1_000_000), reserved, name)
			require.Equal(t, 1, observations, name)
			require.Zero(t, actual, name)
			var reported bool
			require.NoError(t, conn.QueryRow(ctx, "SELECT usage_reported FROM usage_observations WHERE dispatch_id = (SELECT dispatch_id FROM dispatches WHERE call_id = $1)", invalid).Scan(&reported))
			require.False(t, reported, "%s: no counters were reported to the ledger", name)
			// A reentry replays the recorded outcome and reports nothing more.
			r = postModelCall(t, proxyURLA, proxyTokenWorkflow, modelRequest(invalid, opID, attemptID, "", deadline), 0)
			require.Equal(t, "USAGE_INVALID", *r.frames[len(r.frames)-1].ErrorCode, name)
			_, _, observations, _ = dispatchOf(invalid)
			require.Equal(t, 1, observations, name)
		}
		yes := true
		up.next(upstreamBehavior{kind: "stream", text: []string{"free"}, usage: &yes})
		zeroOp, zeroAttempt := e.fundedOperation(ctx, t, opsA, execA, run, "zero", 2_000_000)
		zero := "call-" + run + "-zero-usage"
		r = postModelCall(t, proxyURLB, proxyTokenWorkflow, modelRequest(zero, zeroOp, zeroAttempt, "", deadline), 0)
		require.Equal(t, "done", frameTypes(r.frames)[len(r.frames)-1])
		state, _, observations, actual := dispatchOf(zero)
		require.Equal(t, "observed", state)
		require.Equal(t, 1, observations)
		require.Zero(t, actual)
		require.Zero(t, reservedOf(zeroOp), "explicit zero usage settles the reservation")
	})

	t.Run("P1.1: a 421 answered once is one receive, one dispatch and one send; the re-send the platform fetch would make never leaves the Proxy", func(t *testing.T) {
		// The second scripted answer is what a re-sent request would consume.
		up.next(upstreamBehavior{kind: "status", status: 421}, upstreamBehavior{kind: "stream"})
		before := up.receives.Load()
		callID := "call-" + run + "-421"
		r := postModelCall(t, proxyURLA, proxyTokenWorkflow, modelRequest(callID, opID, attemptID, "", deadline), 0)
		require.Equal(t, "error", frameTypes(r.frames)[len(r.frames)-1])
		require.Equal(t, "UPSTREAM_STATUS_421", *r.frames[len(r.frames)-1].ErrorCode)
		require.Equal(t, before+1, up.receives.Load(), "a 421 reaches the upstream once: no transport re-send")
		require.Equal(t, 1, up.pending(), "the answer a re-sent request would have consumed is still scripted")
		state, reserved, observations, actual := dispatchOf(callID)
		require.Equal(t, "unknown", state, "the single 421 response establishes no usage: the outcome stays unknown, the exposure retained")
		require.Equal(t, int64(1_000_000), reserved)
		require.Equal(t, 1, observations)
		require.Zero(t, actual)
		ev := evidenceOf(callID)
		require.EqualValues(t, 1, ev["wireAttempts"], "the evidence counts the one dispatch that reached the wire")
		require.EqualValues(t, 421, ev["response"].(map[string]any)["status"])
		require.Nil(t, ev["refusedSends"], "nothing beneath the vendor client tried a second send")
		// The unconsumed answer is taken by a fresh admission, not by a retry of this one.
		fresh := "call-" + run + "-421-after"
		r = postModelCall(t, proxyURLB, proxyTokenWorkflow, modelRequest(fresh, opID, attemptID, "", deadline), 0)
		require.Equal(t, "done", frameTypes(r.frames)[len(r.frames)-1])
		require.Equal(t, before+2, up.receives.Load())
		require.Zero(t, up.pending())
		require.EqualValues(t, 1, evidenceOf(fresh)["wireAttempts"])
	})

	t.Run("P1.3: a rollout with a send in flight drains: readiness drops, new opens are refused as retryable, the held send settles with its usage, nothing is SENDER_LOST", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "stream-then-hold", text: []string{"draining"}})
		before := up.receives.Load()
		callID := "call-" + run + "-rollout"
		done := make(chan modelCallReply, 1)
		go func() {
			done <- postModelCall(t, proxyURLB, proxyTokenWorkflow, modelRequest(callID, opID, attemptID, "", deadline), 0)
		}()
		up.awaitHeld(t)
		// SIGTERM to Proxy B while its send is held: the drain begins.
		stopped := make(chan struct{})
		go func() { proxyB.stop(); close(stopped) }()
		require.Eventually(t, func() bool {
			resp, err := http.Get(healthURLB + "/readyz")
			if err != nil {
				return true // the listener is already closed to new connections
			}
			resp.Body.Close()
			return resp.StatusCode == 503
		}, 10*time.Second, 50*time.Millisecond, "readiness is withdrawn first")
		// A new call during the drain is refused as retryable, or the listener no longer accepts: nothing is admitted.
		newReq := modelRequest("call-"+run+"-during-drain", opID, attemptID, "", deadline)
		raw, _ := json.Marshal(newReq)
		httpReq, _ := http.NewRequest("POST", proxyURLB+"/api/v1/model-calls", strings.NewReader(string(raw)))
		httpReq.Header.Set("Authorization", "Bearer "+proxyTokenWorkflow)
		httpReq.Header.Set("Content-Type", "application/json")
		if resp, err := (&http.Client{Timeout: 5 * time.Second}).Do(httpReq); err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			require.Equal(t, 503, resp.StatusCode, string(body))
			require.Contains(t, string(body), `"retryable":true`)
		}
		require.Equal(t, before+1, up.receives.Load(), "the drain sent nothing new")
		// The upstream completes the held answer: the draining process settles it.
		up.release()
		r := <-done
		require.Equal(t, 200, r.status)
		require.Equal(t, "done", frameTypes(r.frames)[len(r.frames)-1], "the caller receives the outcome from the draining process")
		select {
		case <-stopped:
		case <-time.After(30 * time.Second):
			t.Fatal("proxy-b did not exit after its drain")
		}
		state, _, observations, actual := dispatchOf(callID)
		require.Equal(t, "observed", state)
		require.Equal(t, 1, observations)
		require.Equal(t, int64(100*3+50*15), actual, "the usage of the drained send reached the ledger")
		ev := evidenceOf(callID)
		require.Equal(t, "succeeded", ev["settlement"].(map[string]any)["outcome"])
		require.NotEqual(t, "SENDER_LOST", ev["settlement"].(map[string]any)["errorCode"])
		// Proxy B returns for the scenarios that follow.
		proxyB.start()
	})

	t.Run("a cancel on the other Proxy aborts the send; the outcome stays unknown without usage", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "stream-then-hang", text: []string{"holding"}})
		before := up.receives.Load()
		callID := "call-" + run + "-cancel"
		req := modelRequest(callID, opID, attemptID, "", deadline)
		done := make(chan modelCallReply, 1)
		go func() { done <- postModelCall(t, proxyURLA, proxyTokenWorkflow, req, 0) }()
		up.awaitHeld(t)
		require.Equal(t, 202, cancelModelCall(t, proxyURLB, proxyTokenWorkflow, callID))
		r := <-done
		require.Equal(t, "error", frameTypes(r.frames)[len(r.frames)-1])
		require.Equal(t, "CANCELED", *r.frames[len(r.frames)-1].ErrorCode)
		require.Equal(t, before+1, up.receives.Load())
		state, reserved, _, _ := dispatchOf(callID)
		require.Equal(t, "unknown", state)
		require.Equal(t, int64(1_000_000), reserved)
		up.release()
	})

	var lostCall string
	t.Run("a Proxy lost mid-send is reclaimed as unknown by the other after the deadline, never resent", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "stream-then-hang", text: []string{"partial"}})
		before := up.receives.Load()
		lostCall = "call-" + run + "-process-loss"
		req := modelRequest(lostCall, opID, attemptID, "", time.Now().Add(6*time.Second))
		partial := postModelCall(t, proxyURLA, proxyTokenWorkflow, req, 2)
		require.Len(t, partial.frames, 2)
		proxyA.kill()
		up.release()
		require.Eventually(t, func() bool {
			code, m := getModelCall(t, proxyURLB, proxyTokenWorkflow, lostCall)
			return code == 200 && m.State == modelproxyapi.ModelCallStateUnknown
		}, 30*time.Second, 250*time.Millisecond, "the sweep reclaimed the lost send")
		code, m := getModelCall(t, proxyURLB, proxyTokenWorkflow, lostCall)
		require.Equal(t, 200, code)
		require.Equal(t, "SENDER_LOST", *m.ErrorCode)
		require.Equal(t, before+1, up.receives.Load(), "the killed send is the only one")
		require.Eventually(t, func() bool {
			state, _, _, _ := dispatchOf(lostCall)
			return state == "unknown"
		}, 20*time.Second, 250*time.Millisecond, "Control was told the outcome is unknown")
		_, reserved, _, _ := dispatchOf(lostCall)
		require.Equal(t, int64(1_000_000), reserved)
		proxyA.start()
		replay := postModelCall(t, proxyURLA, proxyTokenWorkflow, req, 0)
		require.Equal(t, 200, replay.status)
		require.Equal(t, []string{"admitted", "error"}, frameTypes(replay.frames))
		require.Equal(t, before+1, up.receives.Load())
	})

	t.Run("a lost observation answer is resubmitted under the original identity once Control returns", func(t *testing.T) {
		up.next(upstreamBehavior{kind: "stream-then-hold", text: []string{"observed later"}})
		before := up.receives.Load()
		callID := "call-" + run + "-lost-observation"
		req := modelRequest(callID, opID, attemptID, "", deadline)
		done := make(chan modelCallReply, 1)
		go func() { done <- postModelCall(t, proxyURLB, proxyTokenWorkflow, req, 0) }()
		up.awaitHeld(t)
		e.kill("control-b")
		restarted := false
		restart := func() {
			if !restarted {
				restarted = true
				e.start("control-b")
				e.waitTCP(e.controlAddrB)
			}
		}
		defer restart()
		up.release()
		r := <-done
		require.Equal(t, 200, r.status, string(r.body))
		require.Equal(t, "done", frameTypes(r.frames)[len(r.frames)-1], "the caller receives the outcome while Control is down")
		require.Equal(t, before+1, up.receives.Load())
		_, err := os.Stat(filepath.Join(storeDir, "pending", callID, "tenant_a"))
		require.NoError(t, err, "the observation stays pending under its recorded identity")
		restart()
		require.Eventually(t, func() bool {
			state, _, observations, _ := dispatchOf(callID)
			return state == "observed" && observations == 1
		}, 30*time.Second, 250*time.Millisecond, "the Proxy resubmitted the observation")
		require.Eventually(t, func() bool {
			_, err := os.Stat(filepath.Join(storeDir, "pending", callID, "tenant_a"))
			return os.IsNotExist(err)
		}, 10*time.Second, 200*time.Millisecond)
		require.Equal(t, before+1, up.receives.Load())
	})

	t.Run("recovery queries the Proxy about the lost send under its original identity and keeps it unknown", func(t *testing.T) {
		if lostCall == "" {
			skipMissing(t, "subtest:process-loss", "the process-loss scenario did not run")
		}
		var dispatchID string
		require.NoError(t, conn.QueryRow(ctx, "SELECT dispatch_id FROM dispatches WHERE call_id = $1", lostCall).Scan(&dispatchID))
		// Operator commands arrive through the API; the run's steps are the Workflow's.
		recA := controlv1.NewRecoveryServiceClient(e.dialControl(e.controlAddr, principalAPI))
		stepsA := controlv1.NewRecoveryServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
		operator := &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "recover-" + run + "-proxy", ActorId: "integration-operator", RequestDigest: digestOf("recover-proxy-" + run)}
		began, err := recA.BeginRecovery(ctx, &controlv1.BeginRecoveryRequest{
			Scope: operatorScope("tenant_a", "integration-operator"), Command: operator, ScopeKey: "tenant_a", WindowStart: timestamppb.New(time.Now().Add(-time.Hour)), WindowEnd: timestamppb.New(time.Now().Add(time.Minute)),
			ClockUncertainty: durationpb.New(5 * time.Second), Reason: "P11 original-identity query",
		})
		require.NoError(t, err)
		runID := began.GetRun().GetRunId()
		// Whatever happens below, the scope must not stay closed behind this
		// scenario: the shared development database serves every other one.
		t.Cleanup(func() {
			if got, err := recA.GetRecovery(context.Background(), &controlv1.GetRecoveryRequest{RunId: runID, Scope: operatorScope("tenant_a", "integration-operator")}); err == nil && got.GetRun().GetPhase() != controlv1.RecoveryPhase_RECOVERY_PHASE_REOPENED {
				t.Logf("recovery run %s left in phase %s; reopening its closure directly", runID, got.GetRun().GetPhase())
				_, _ = conn.Exec(context.Background(), "UPDATE admission_closures SET reopened_at = now() WHERE run_id = $1 AND reopened_at IS NULL", runID)
				_, _ = conn.Exec(context.Background(), "UPDATE recovery_runs SET phase = 'reopened', reopened_at = now(), updated_at = now() WHERE run_id = $1 AND phase <> 'reopened'", runID)
			}
		})
		// The RecoveryWorkflow's steps, called directly: every class is
		// enumerated completely, every finding reconciled.
		for _, class := range []string{"intake", "job-launch", "model-dispatch", "tool-dispatch", "business-write"} {
			for {
				resp, err := stepsA.EnumerateInventory(ctx, &controlv1.EnumerateInventoryRequest{RunId: runID, Class: class})
				require.NoError(t, err)
				if resp.GetProgress().GetComplete() {
					break
				}
			}
		}
		listAll := func() []*controlv1.Finding {
			var out []*controlv1.Finding
			cursor := ""
			for {
				list, err := stepsA.ListFindings(ctx, &controlv1.ListFindingsRequest{RunId: runID, Limit: 200, Cursor: cursor})
				require.NoError(t, err)
				out = append(out, list.GetFindings()...)
				if list.GetComplete() || list.GetNextCursor() == "" {
					return out
				}
				cursor = list.GetNextCursor()
			}
		}
		var lost *controlv1.Finding
		var unresolved []*controlv1.Finding
		for _, f := range listAll() {
			settled := f.GetStatus() == controlv1.FindingStatus_FINDING_STATUS_PRESENT || f.GetStatus() == controlv1.FindingStatus_FINDING_STATUS_RESOLVED
			if !settled {
				rec, err := stepsA.ReconcileFinding(ctx, &controlv1.ReconcileFindingRequest{RunId: runID, FindingId: f.GetFindingId()})
				require.NoError(t, err, "reconciling %s finding %s", f.GetClass(), f.GetObligationId())
				f = rec.GetFinding()
			}
			if f.GetObligationId() == dispatchID {
				lost = f
			}
			if f.GetStatus() == controlv1.FindingStatus_FINDING_STATUS_UNRESOLVED {
				unresolved = append(unresolved, f)
			}
			require.NotEqual(t, controlv1.FindingStatus_FINDING_STATUS_MISSING, f.GetStatus(), "%v", f)
		}
		require.NotNil(t, lost, "the lost send is in the window")
		require.Equal(t, controlv1.FindingStatus_FINDING_STATUS_UNRESOLVED, lost.GetStatus())
		require.Contains(t, lost.GetDetail(), "upstream reports an unknown outcome", "the Proxy answered the original identity: unknown, exposure retained (not 'no upstream record'): %v", lost)
		state, reserved, _, _ := dispatchOf(lostCall)
		require.Equal(t, "unknown", state)
		require.Equal(t, int64(1_000_000), reserved)
		var querySources []string
		rows, err := conn.Query(ctx, "SELECT source FROM usage_observations WHERE dispatch_id = $1 ORDER BY observed_at", dispatchID)
		require.NoError(t, err)
		for rows.Next() {
			var src string
			require.NoError(t, rows.Scan(&src))
			querySources = append(querySources, src)
		}
		rows.Close()
		require.Contains(t, querySources, "query:model-proxy", "the query's answer is recorded as an observation under its own source")
		// The operator accepts the retained exposure of every unresolved send
		// (trusted disposition attestations in the inventory) so the scope
		// reopens; the dispatches stay unknown with their exposure. Nothing is
		// resent.
		before := up.receives.Load()
		dispositionDir := filepath.Join(e.local, "inventory", "disposition", "model-dispatch")
		require.NoError(t, os.MkdirAll(dispositionDir, 0o750))
		for i, f := range unresolved {
			require.Equal(t, "model-dispatch", f.GetClass(), "only model sends of this scenario stay unresolved: %v", f)
			attestation, _ := json.Marshal(map[string]string{"class": "disposition", "obligationClass": "model-dispatch", "obligationId": f.GetObligationId(), "decision": "retain_exposure", "issuer": "integration-supervisor", "sealedAt": time.Now().UTC().Format(time.RFC3339)})
			require.NoError(t, os.WriteFile(filepath.Join(dispositionDir, f.GetObligationId()), attestation, 0o640))
			_, err = recA.DisposeObligation(ctx, &controlv1.DisposeObligationRequest{
				Scope:   operatorScope("tenant_a", "integration-operator"),
				Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: fmt.Sprintf("dispose-%s-%d", run, i), ActorId: "integration-operator", RequestDigest: digestOf("dispose-" + f.GetObligationId())},
				Class:   "model-dispatch", ObligationId: f.GetObligationId(), Decision: controlv1.DispositionDecision_DISPOSITION_DECISION_RETAIN_EXPOSURE,
				EvidenceRef: "disposition/model-dispatch/" + f.GetObligationId(), EvidenceDigest: digestOf(string(attestation)), RecoveryEpoch: began.GetRun().GetRecoveryEpoch(), RunId: &runID, Reason: "P11: the sender was lost or the upstream outcome is unknown; the exposure stays",
			})
			require.NoError(t, err, "disposing %s", f.GetObligationId())
		}
		evaluated, err := stepsA.EvaluateRecovery(ctx, &controlv1.EvaluateRecoveryRequest{RunId: runID})
		require.NoError(t, err)
		require.Equal(t, controlv1.RecoveryPhase_RECOVERY_PHASE_REOPENED, evaluated.GetRun().GetPhase(), "every finding settled: %v", evaluated.GetRun())
		state, reserved, _, _ = dispatchOf(lostCall)
		require.Equal(t, "unknown", state)
		require.Equal(t, int64(1_000_000), reserved, "the disposition retains the exposure")
		require.Equal(t, before, up.receives.Load())
	})
}

func logTail(t *testing.T, path string, n int) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

func readLog(t *testing.T, p *proxyProcess) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(p.cfg), p.name+".log"))
	require.NoError(t, err)
	return raw
}

// relayCall posts one model relay over trusted.sock and returns the status and the raw answer.
func relayCall(t *testing.T, socket, body string) (int, []byte) {
	t.Helper()
	tr := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}, DisableKeepAlives: true}
	c := &http.Client{Transport: tr, Timeout: 2 * time.Minute}
	req, err := http.NewRequest("POST", "http://sidecar/v1/model/relay", strings.NewReader(body))
	require.NoError(t, err)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer candidate-supplied-never-forwarded")
	resp, err := c.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, raw
}
