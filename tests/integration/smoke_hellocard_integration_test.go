//go:build integration && smoke

// Development smoke test (DEVELOPMENT_ONLY, not a G gate): one real model
// generation of one HelloCard component through the controlled send path
// (host-built Control with its admission and budget, host-built Model Proxy
// holding the only provider credential), the Pi coder of the codegen team
// as the one source writer behind a small forwarding double of the access
// sidecar, the trusted validator's build of the generated source, and one
// browser screenshot of the built artifact. It runs only under the explicit
// build tags `integration smoke` (tools/smoke/hello-card.sh), never in the
// ordinary integration suite, because a real route spends money.
package integration

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	"github.com/ancyloce/anvilkit-agent-contracts/go/modelproxyapi"
)

const (
	smokeRouteID  = "hello-card-smoke"
	smokeTenant   = "tenant_a"
	smokeCurrency = "USD"
	// smokeExposurePerSend is the route's reviewed full bound, reserved by
	// the Proxy for every send and declared by the coder for every call
	// (scale-6 USD: 200000 = USD 0.20).
	smokeExposurePerSend = int64(200_000)
	// smokeCeiling is the pool cap of the run (USD 5, the authorized
	// live-provider ceiling of deploy/pi-upgrade/README.md).
	smokeCeiling = int64(5_000_000)
)

// smokeCheck is one of the three checks the smoke reports.
type smokeCheck struct {
	Name   string `json:"name"`
	Status string `json:"status"` // PASS, FAIL, UNEXECUTED
	Detail string `json:"detail"`
}

type smokeSummary struct {
	Mode             string       `json:"mode"`
	Route            string       `json:"route"`
	Provider         string       `json:"provider"`
	Model            string       `json:"model"`
	Title            string       `json:"title"`
	RelayCalls       int          `json:"relayCalls"`
	ProxyCalls       int          `json:"proxyCalls"`
	ControlDispatchs int          `json:"controlDispatches"`
	Checks           []smokeCheck `json:"checks"`
	EvidenceDir      string       `json:"evidenceDir"`
	StartedAt        string       `json:"startedAt"`
	CompletedAt      string       `json:"completedAt"`
}

func envOr(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

func randomToken(t *testing.T) string {
	t.Helper()
	b := make([]byte, 16)
	_, err := rand.Read(b)
	require.NoError(t, err)
	return hex.EncodeToString(b)
}

// smokeControlConfig is Control's checked-in configuration with the smoke
// route's price observation and authorization added to the DEVELOPMENT_ONLY
// dispatch fixture. The prices are a conservative ceiling for the budget
// guard (overstated on purpose), never a provider invoice.
func smokeControlConfig(t *testing.T, root, dir, provider, model string) string {
	t.Helper()
	base, err := os.ReadFile(filepath.Join(root, serviceDirs["anvilkit-agent-control"], "config.yaml"))
	require.NoError(t, err)
	text := string(base)
	fixtures := fmt.Sprintf(`  development:
    enabled: true
    prices:
      - revision: smoke-hello-card-model
        kind: model
        route: %s
        provider: %s
        model: %s
        currency: USD
        effective_from: 2026-01-01T00:00:00Z
        effective_until: 2027-01-01T00:00:00Z
        per_million: {input_units: "1000000", output_units: "3000000", reasoning_units: "3000000", cached_input_units: "100000"}
        max_exposure: "%d"
    authorized_routes:
      - tenant_id: %s
        route_id: %s
    not_sent_issuers: [smoke-hello-card]`, smokeRouteID, provider, model, smokeExposurePerSend, smokeTenant, smokeRouteID)
	require.Contains(t, text, "  development:\n    enabled: true")
	text = strings.Replace(text, "  development:\n    enabled: true", fixtures, 1)
	path := filepath.Join(dir, "control.smoke.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

// smokeProxyConfig is the Proxy's checked-in configuration with the bearer
// identity of the development foundation and exactly one route: the smoke
// route, carrying the coder's reviewed file tools (agent/team/tools.json),
// so the Proxy serves them by their exact schema digest.
func smokeProxyConfig(t *testing.T, root, dir, listen, provider, model, baseURL string, tools []string) string {
	t.Helper()
	base, err := os.ReadFile(filepath.Join(root, "services", "agent", "model-proxy", "config.yaml"))
	require.NoError(t, err)
	text := string(base)
	for _, r := range [][2]string{
		{"mode: disabled", "mode: development"},
		{"listen: 127.0.0.1:9103", "listen: " + listen},
		{"  sse_heartbeat: 15s", "  sse_heartbeat: 5s"},
	} {
		require.Contains(t, text, r[0])
		text = strings.Replace(text, r[0], r[1], 1)
	}
	i := strings.Index(text, "\nroutes:\n")
	require.Positive(t, i, "the checked-in Proxy configuration declares routes")
	text = text[:i+1]
	raw, err := os.ReadFile(filepath.Join(teamRepo(root), "agent", "team", "tools.json"))
	require.NoError(t, err)
	var doc struct {
		Tools []struct {
			Name        string          `json:"name"`
			Description string          `json:"description"`
			InputSchema json.RawMessage `json:"input_schema"`
		} `json:"tools"`
	}
	require.NoError(t, json.Unmarshal(raw, &doc))
	wanted := map[string]bool{}
	for _, n := range tools {
		wanted[n] = true
	}
	var b strings.Builder
	fmt.Fprintf(&b, "routes:\n  - id: %s\n    enabled: true\n    api: openai-completions\n    provider: %s\n    model: %s\n    base_url: %s\n    credential_env: ANVILKIT_MODEL_PROXY_CREDENTIAL_HELLO_CARD_SMOKE\n", smokeRouteID, provider, model, baseURL)
	fmt.Fprintf(&b, "    limits:\n      max_output_tokens: 4096\n      max_exposure: { currency: USD, amount: \"%d\" }\n      max_deadline: 10m\n      max_frame_bytes: 65536\n      max_output_bytes: 1048576\n      max_frames: 4096\n      upstream_timeout: 4m\n      max_evidence_bytes: 4194304\n    tools:\n", smokeExposurePerSend)
	n := 0
	for _, tool := range doc.Tools {
		if !wanted[tool.Name] {
			continue
		}
		desc, _ := json.Marshal(tool.Description)
		var schema bytes.Buffer
		require.NoError(t, json.Compact(&schema, tool.InputSchema))
		fmt.Fprintf(&b, "      - name: %s\n        description: %s\n        input_schema: %s\n", tool.Name, desc, schema.String())
		n++
	}
	require.Equal(t, len(tools), n, "every coder tool is in agent/team/tools.json")
	path := filepath.Join(dir, "model-proxy.smoke.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text+b.String()), 0o600))
	return path
}

// relayDouble is the smoke's stand-in for the access sidecar's controlled
// model relay (jobs/shared/access-sidecar/internal/scope/relay.go): it
// binds every relay request of the coder to the funded attempt's execution
// binding and forwards it, once, to the real Model Proxy with the sidecar
// principal. It holds no provider credential and executes no tool.
type relayDouble struct {
	socket, proxyURL, token string
	binding                 modelproxyapi.ExecutionBinding
	deadline                time.Time
	mu                      sync.Mutex
	calls                   []smokeRelayCall
	srv                     *http.Server
	logf                    *os.File
}

type smokeRelayCall struct {
	CallID string `json:"callId"`
	Status int    `json:"status"`
	Final  string `json:"final"` // the final frame's type:outcome, or the refusal body's code
	Frames int    `json:"frames"`
}

type relayRequest struct {
	CallID          string                         `json:"callId"`
	RouteID         string                         `json:"routeId"`
	Messages        []modelproxyapi.Message        `json:"messages"`
	Tools           []modelproxyapi.ToolDefinition `json:"tools,omitempty"`
	MaxOutputTokens int                            `json:"maxOutputTokens"`
	MaxExposure     modelproxyapi.Money            `json:"maxExposure"`
	Deadline        string                         `json:"deadline,omitempty"`
}

func (d *relayDouble) start(t *testing.T) {
	t.Helper()
	ln, err := net.Listen("unix", d.socket)
	require.NoError(t, err)
	d.srv = &http.Server{Handler: http.HandlerFunc(d.handle)}
	go func() { _ = d.srv.Serve(ln) }()
	t.Cleanup(func() { _ = d.srv.Close() })
}

func (d *relayDouble) handle(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost || r.URL.Path != "/v1/model/relay" {
		http.Error(w, `{"code":"NOT_FOUND","reason":"not a relay"}`, http.StatusNotFound)
		return
	}
	raw, err := io.ReadAll(io.LimitReader(r.Body, 4<<20))
	if err != nil {
		http.Error(w, `{"code":"INVALID_ARGUMENT","reason":"body"}`, http.StatusBadRequest)
		return
	}
	var rr relayRequest
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&rr); err != nil || rr.CallID == "" || rr.RouteID == "" || len(rr.Messages) == 0 {
		http.Error(w, `{"code":"INVALID_ARGUMENT","reason":"relay request"}`, http.StatusBadRequest)
		return
	}
	deadline := d.deadline
	if rr.Deadline != "" {
		if requested, err := time.Parse(time.RFC3339Nano, rr.Deadline); err == nil && requested.Before(deadline) {
			deadline = requested
		}
	}
	bound, _ := json.Marshal(d.binding)
	req := modelproxyapi.ModelCallRequest{
		CallId: rr.CallID, Binding: d.binding, RouteId: rr.RouteID,
		RequestDigest:   digestOfBytes(append(append(bound, '\n'), raw...)),
		Messages:        rr.Messages,
		MaxOutputTokens: rr.MaxOutputTokens, MaxExposure: rr.MaxExposure,
		Deadline: deadline.UTC().Truncate(time.Millisecond).Format("2006-01-02T15:04:05.000Z"),
	}
	if len(rr.Tools) > 0 {
		tools := rr.Tools
		req.Tools = &tools
	}
	body, _ := json.Marshal(req)
	ctx, cancel := context.WithDeadline(r.Context(), deadline.Add(30*time.Second))
	defer cancel()
	httpReq, _ := http.NewRequestWithContext(ctx, http.MethodPost, d.proxyURL+"/api/v1/model-calls", bytes.NewReader(body))
	httpReq.Header.Set("Authorization", "Bearer "+d.token)
	httpReq.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(httpReq)
	call := smokeRelayCall{CallID: rr.CallID}
	if err != nil {
		call.Status, call.Final = http.StatusServiceUnavailable, "UNAVAILABLE"
		d.record(call)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"code":"UNAVAILABLE","reason":"model proxy unreachable"}`))
		return
	}
	defer resp.Body.Close()
	call.Status = resp.StatusCode
	w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
	w.Header().Set("Connection", "close")
	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
		var env struct {
			Error struct{ Code string } `json:"error"`
		}
		_ = json.Unmarshal(b, &env)
		call.Final = "refused:" + env.Error.Code
		d.record(call)
		w.WriteHeader(resp.StatusCode)
		_, _ = w.Write(b)
		return
	}
	w.WriteHeader(http.StatusOK)
	rc := http.NewResponseController(w)
	_ = rc.Flush()
	reader := bufio.NewReaderSize(resp.Body, 64<<10)
	for {
		line, err := reader.ReadSlice('\n')
		if len(line) > 0 {
			if bytes.HasPrefix(line, []byte("data:")) {
				var f modelproxyapi.StreamFrame
				if json.Unmarshal(bytes.TrimSpace(line[5:]), &f) == nil {
					call.Frames++
					if f.Type == modelproxyapi.StreamFrameTypeDone || f.Type == modelproxyapi.StreamFrameTypeError {
						outcome := ""
						if f.Outcome != nil {
							outcome = string(*f.Outcome)
						}
						call.Final = string(f.Type) + ":" + outcome
						if f.ErrorCode != nil {
							call.Final += ":" + *f.ErrorCode
						}
					}
				}
			}
			if _, werr := w.Write(line); werr != nil {
				d.record(call)
				return
			}
			if len(line) == 1 {
				_ = rc.Flush()
			}
		}
		if err != nil {
			_ = rc.Flush()
			d.record(call)
			return
		}
	}
}

func (d *relayDouble) record(c smokeRelayCall) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.calls = append(d.calls, c)
	if d.logf != nil {
		raw, _ := json.Marshal(c)
		_, _ = d.logf.Write(append(raw, '\n'))
	}
}

func (d *relayDouble) snapshot() []smokeRelayCall {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]smokeRelayCall(nil), d.calls...)
}

// helloCardPlan is the frozen brief and plan the coder implements: the
// component source contract the validator enforces (jobs/validator
// src/source.ts), stated as the planner would. The React source, the
// stylesheet, the usage note and the manifests are the model's to write;
// the lockfile text is the trusted build configuration (exact pinned
// dependencies), given verbatim.
func helloCardPlan(title string) string {
	brief := map[string]any{
		"schemaVersion": 1, "componentId": "cmp_hello_card_smoke", "puckType": "HelloCard", "packageName": "@anvilkit/hello-card-smoke", "version": "1.0.0",
		"requirements": "A HelloCard component: a card that displays one title prop as its heading. A simple background colour, a border and padding. No interactions, images, custom fonts, network requests or dependencies beyond react and @puckeditor/core. The default title is \"" + title + "\".",
		"sourceRevision": "1",
	}
	b, _ := json.MarshalIndent(brief, "", "  ")
	return "Implement source revision 1 exactly as planned.\n\nFrozen brief (JSON):\n" + string(b) + `

Plan:
1. component.json with exactly these members and no others:
   {"schemaVersion":1,"componentId":"cmp_hello_card_smoke","puckType":"HelloCard","entry":"src/index.tsx","styles":["styles/hello-card.css"],"resources":[],"usage":"README.md","editableFields":[{"name":"title","type":"text","default":"` + title + `"}]}
2. package.json with exactly the members name, version, description, license and dependencies: name "@anvilkit/hello-card-smoke", version "1.0.0", license "MIT", a one-sentence description, dependencies exactly {"@puckeditor/core":"0.23.0","react":"19.3.0"} (exact versions, nothing else, no scripts).
3. pnpm-lock.yaml with exactly this content (the pinned build configuration):
lockfileVersion: '9.0'

settings:
  autoInstallPeers: true
  excludeLinksFromLockfile: false

importers:

  .:
    dependencies:
      '@puckeditor/core':
        specifier: 0.23.0
        version: 0.23.0
      react:
        specifier: 19.3.0
        version: 19.3.0

4. README.md: a short usage note (the default export is the Puck ComponentConfig; load styles/hello-card.css before importing the module).
5. src/hello-card.tsx: export interface HelloCardProps { title: string } and export function HelloCard({ title }: HelloCardProps) rendering a <section className="ak-hello-card" data-anvilkit-component="HelloCard"> with an <h2 className="ak-hello-card__title"> showing the title prop. No state, no handlers, no images, no fonts, no fetches.
6. src/index.tsx: import type { ComponentConfig } from "@puckeditor/core"; export const config: ComponentConfig<HelloCardProps> = { label: "HelloCard", fields: { title: { type: "text" } }, defaultProps: { title: "` + title + `" }, render: ({ title }) => <HelloCard title={title} /> }; export { HelloCard }; export type { HelloCardProps }; export default config;
7. styles/hello-card.css: .ak-hello-card with a background colour, a 1px solid border, a border radius and padding; .ak-hello-card__title with a margin and font size. Plain CSS, no @import, no url().

Write every file completely with the write tool. Strict TypeScript, react-jsx; import only react, react/jsx-runtime, @puckeditor/core and relative files under src/. No other files, no directories besides src/ and styles/. When all seven files exist, answer with one short sentence stating that the source is complete.`
}

func TestSmokeHelloCard(t *testing.T) {
	startedAt := time.Now().UTC()
	root := repoRoot(t)
	mode := envOr("ANVILKIT_SMOKE_UPSTREAM", "real") // real | fixture (a countable double; a dry run of the harness, never a generation claim)
	title := envOr("ANVILKIT_SMOKE_TITLE", "AnvilKit smoke test passed")
	provider, model, baseURL := envOr("ANVILKIT_SMOKE_PROVIDER", "deepseek"), envOr("ANVILKIT_SMOKE_MODEL", "deepseek-flash"), envOr("ANVILKIT_SMOKE_BASE_URL", "https://api.deepseek.com")
	maxCalls := 8
	evidence := envOr("ANVILKIT_SMOKE_EVIDENCE_DIR", filepath.Join(root, "outputs", "smoke-hello-card", startedAt.Format("20060102T150405Z")))
	require.NoError(t, os.MkdirAll(evidence, 0o700))
	coderTools := []string{"read", "write", "edit", "ls", "find"} // grep needs ripgrep, which the host does not provide
	summary := smokeSummary{Mode: mode, Route: smokeRouteID, Provider: provider, Model: model, Title: title, EvidenceDir: evidence, StartedAt: startedAt.Format(time.RFC3339),
		Checks: []smokeCheck{{Name: "model_generated_source", Status: "UNEXECUTED"}, {Name: "generated_source_builds", Status: "UNEXECUTED"}, {Name: "title_rendered_in_browser", Status: "UNEXECUTED"}}}
	writeSummary := func() {
		summary.CompletedAt = time.Now().UTC().Format(time.RFC3339)
		raw, _ := json.MarshalIndent(summary, "", "  ")
		_ = os.WriteFile(filepath.Join(evidence, "summary.json"), raw, 0o600)
	}
	defer writeSummary()
	set := func(i int, status, detail string) {
		summary.Checks[i].Status, summary.Checks[i].Detail = status, detail
		t.Logf("check %s: %s (%s)", summary.Checks[i].Name, status, detail)
	}

	// The provider credential: read once from the named file, handed to the
	// Model Proxy process only, never logged or written.
	var credential string
	switch mode {
	case "fixture":
		provider, model = "fixture", "fixture-model"
		summary.Provider, summary.Model = provider, model
		credential = "smoke-fixture-credential"
	case "real":
		credFile := os.Getenv("ANVILKIT_SMOKE_CREDENTIAL_FILE")
		if credFile == "" {
			skipMissing(t, "ANVILKIT_SMOKE_CREDENTIAL_FILE", "not set: the path of a 0600 file holding the provider API key (injected into the Model Proxy only)")
		}
		raw, err := os.ReadFile(credFile)
		require.NoError(t, err)
		credential = strings.TrimSpace(string(raw))
		if credential == "" {
			skipMissing(t, "ANVILKIT_SMOKE_CREDENTIAL_FILE", "the credential file is empty")
		}
	default:
		t.Fatalf("ANVILKIT_SMOKE_UPSTREAM must be real or fixture, not %q", mode)
	}
	if _, err := os.Stat(filepath.Join(teamRepo(root), "dist", "coder.js")); err != nil {
		skipMissing(t, "build:jobs/codegen/team/dist", "jobs/codegen/team/dist is not built (pnpm run build in the team repository)")
	}
	validatorPkg := filepath.Join(root, "jobs", "validator")
	if _, err := os.Stat(filepath.Join(validatorPkg, "dist", "cli.js")); err != nil {
		skipMissing(t, "build:jobs/validator/dist", "jobs/validator/dist is not built (pnpm run build in the validator repository)")
	}

	// Control on a disposable database, funded for this one operation.
	isolatedControlDatabase(t)
	scratch := t.TempDir()
	port := freePort(t)
	proxyURL, healthURL := "http://127.0.0.1:"+port, "http://127.0.0.1:"+freePort(t)
	tokenWorkflow, tokenSidecar, tokenControl := randomToken(t), randomToken(t), randomToken(t)
	e := setupProcessesEnv(t, false, "", smokeControlConfig(t, root, scratch, provider, model), true,
		[]string{"ANVILKIT_CONTROL_MODEL_PROXY_ADDRESS=" + proxyURL, "ANVILKIT_CONTROL_MODEL_PROXY_TOKEN=" + tokenControl})
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Minute)
	defer cancel()
	upstreamURL := baseURL
	var up *modelUpstream
	if mode == "fixture" {
		up = newModelUpstream(t)
		upstreamURL = up.srv.URL + "/v1"
	}

	// The Model Proxy: the only process that holds the credential.
	dist := buildModelProxy(t, root)
	principals := filepath.Join(scratch, "model-proxy-principals.json")
	require.NoError(t, os.WriteFile(principals, []byte(fmt.Sprintf(`{%q:{"principalId":"anvilkit-agent-workflow","kind":"workflow"},%q:{"principalId":"anvilkit-job-access-sidecar","kind":"sidecar"},%q:{"principalId":"anvilkit-agent-control","kind":"control"}}`, tokenWorkflow, tokenSidecar, tokenControl)), 0o600))
	storeDir := filepath.Join(scratch, "store")
	cfg := smokeProxyConfig(t, root, scratch, "127.0.0.1:"+port, provider, model, upstreamURL, coderTools)
	proxyLog, err := os.OpenFile(filepath.Join(evidence, "model-proxy.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	require.NoError(t, err)
	proxy := exec.Command("node", dist)
	proxy.Dir = filepath.Join(root, "services", "agent", "model-proxy")
	proxy.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + os.Getenv("HOME"),
		"ANVILKIT_MODEL_PROXY_CONFIG=" + cfg, "ANVILKIT_MODEL_PROXY_CONTROL_ADDRESS=" + e.controlAddr, "ANVILKIT_MODEL_PROXY_PRINCIPALS_FILE=" + principals,
		"ANVILKIT_MODEL_PROXY_HEALTH_LISTEN=" + strings.TrimPrefix(healthURL, "http://"),
		"ANVILKIT_MODEL_PROXY_STORE_DIR=" + storeDir, "ANVILKIT_MODEL_PROXY_CONTRACTS_DIR=" + filepath.Join(root, "contracts"),
		"ANVILKIT_MODEL_PROXY_CREDENTIAL_HELLO_CARD_SMOKE=" + credential}
	proxy.Env = append(proxy.Env, identityEnv(t, "ANVILKIT_MODEL_PROXY_CONTROL_", principalModelProxy)...)
	proxy.Stdout, proxy.Stderr = proxyLog, proxyLog
	require.NoError(t, proxy.Start())
	t.Cleanup(func() {
		_ = proxy.Process.Signal(syscall.SIGTERM)
		done := make(chan struct{})
		go func() { _ = proxy.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(20 * time.Second):
			_ = proxy.Process.Kill()
		}
		proxyLog.Close()
	})
	ready := false
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
		if resp, err := http.Get(healthURL + "/readyz"); err == nil {
			resp.Body.Close()
			if resp.StatusCode == 200 {
				ready = true
				break
			}
		}
	}
	require.True(t, ready, "the Model Proxy became ready:\n%s", logTail(t, filepath.Join(evidence, "model-proxy.log"), 40))

	// Budget: pools at the USD 5 ceiling and one funded operation/attempt
	// whose allocation covers exactly maxCalls sends at the route bound.
	conn := e.db()
	for _, row := range []struct {
		level         string
		tenant, actor *string
	}{{"platform", nil, nil}, {"tenant", strPtr(smokeTenant), nil}, {"actor", strPtr(smokeTenant), strPtr("user_a")}} {
		_, err := conn.Exec(ctx, "INSERT INTO budget_pools (pool_id, level, tenant_id, actor_id, period_start, period_end, currency, cap_amount) VALUES ($1, $2, $3, $4, now() - interval '1 hour', now() + interval '1 hour', 'USD', $5)",
			"pool-"+run+"-"+row.level, row.level, row.tenant, row.actor, smokeCeiling)
		require.NoError(t, err)
	}
	ops, execs := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	opID, attemptID := e.fundedOperation(ctx, t, ops, execs, run, "hello-card", int64(maxCalls)*smokeExposurePerSend)
	t.Logf("operation %s attempt %s funded with %d x %d scale-6 USD", opID, attemptID, maxCalls, smokeExposurePerSend)

	// The coder's layout: a short path for the Unix socket, real directories
	// under the workspace.
	ws, err := os.MkdirTemp("", "akhc")
	require.NoError(t, err)
	t.Cleanup(func() { _ = os.RemoveAll(ws) })
	sourceDir, sessionDir := filepath.Join(ws, "w", "source"), filepath.Join(ws, "w", "session")
	roundDir := filepath.Join(ws, "round", "1")
	require.NoError(t, os.MkdirAll(roundDir, 0o700))
	deadline := time.Now().Add(8 * time.Minute)
	relayLog, err := os.OpenFile(filepath.Join(evidence, "relay-calls.jsonl"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	require.NoError(t, err)
	defer relayLog.Close()
	relay := &relayDouble{socket: filepath.Join(ws, "c.sock"), proxyURL: proxyURL, token: tokenSidecar, deadline: deadline, logf: relayLog,
		binding: modelproxyapi.ExecutionBinding{TenantId: smokeTenant, OperationId: opID, AttemptId: attemptID, ExecutionEpoch: "1"}}
	relay.start(t)
	if mode == "fixture" {
		scriptFixtureCoder(t, up, title)
	}
	systemPrompt, err := os.ReadFile(filepath.Join(teamRepo(root), "agent", "team", "prompts", "coder.md"))
	require.NoError(t, err)
	exposure := modelproxyapi.Money{Currency: smokeCurrency, Amount: fmt.Sprint(smokeExposurePerSend)}
	total := modelproxyapi.Money{Currency: smokeCurrency, Amount: fmt.Sprint(int64(maxCalls) * smokeExposurePerSend)}
	round := map[string]any{
		"schemaVersion": 1, "round": 1, "kind": "code", "callIdPrefix": "smoke-" + run, "routeId": smokeRouteID,
		"model":  map[string]any{"contextWindow": 65536, "maxTokens": 4096},
		"limits": map[string]any{"maxCalls": maxCalls, "maxOutputTokens": 4096, "exposurePerSend": exposure, "exposure": total, "aggregateCalls": maxCalls, "aggregateExposure": total},
		"deadline":   deadline.UTC().Format("2006-01-02T15:04:05.000Z"),
		"compaction": map[string]any{"enabled": false, "reserveTokens": 16384, "keepRecentTokens": 8192},
		"systemPrompt": string(systemPrompt), "tools": coderTools, "prompt": helloCardPlan(title), "sourceRevision": "1",
		"workspace": ws, "sourceDir": sourceDir, "sessionDir": sessionDir, "socket": relay.socket,
	}
	roundRaw, err := json.MarshalIndent(round, "", "  ")
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(filepath.Join(roundDir, "round.json"), roundRaw, 0o600))
	require.NoError(t, os.WriteFile(filepath.Join(evidence, "round.json"), roundRaw, 0o600))

	// Check 1: the coder, one round, one task.
	coderLog, err := os.OpenFile(filepath.Join(evidence, "coder.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	require.NoError(t, err)
	coderCtx, coderCancel := context.WithDeadline(ctx, deadline.Add(30*time.Second))
	defer coderCancel()
	coder := exec.CommandContext(coderCtx, "node", filepath.Join(teamRepo(root), "dist", "coder.js"))
	coder.Dir = teamRepo(root)
	coder.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + roundDir, "ANVILKIT_ROUND_DIR=" + roundDir}
	coder.Stdout, coder.Stderr = coderLog, coderLog
	coderErr := coder.Run()
	coderLog.Close()
	calls := relay.snapshot()
	summary.RelayCalls = len(calls)
	outcomeRaw, outcomeErr := os.ReadFile(filepath.Join(ws, "w", "rounds", "1", "outcome.json"))
	var outcome struct {
		Calls       int    `json:"calls"`
		Ended       string `json:"ended"`
		Error       string `json:"error"`
		FailureCode string `json:"failureCode"`
		Failure     string `json:"failure"`
	}
	if outcomeErr == nil {
		_ = os.WriteFile(filepath.Join(evidence, "outcome.json"), outcomeRaw, 0o600)
		_ = json.Unmarshal(outcomeRaw, &outcome)
	}
	// The generated source, as written (the session transcript stays out of
	// the evidence: it carries the prompt and the model's full output).
	generated := filepath.Join(evidence, "generated-source")
	if _, err := os.Stat(sourceDir); err == nil {
		out, err := exec.Command("cp", "-a", sourceDir, generated).CombinedOutput()
		require.NoError(t, err, string(out))
	}
	var files []string
	_ = filepath.Walk(generated, func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			rel, _ := filepath.Rel(generated, p)
			files = append(files, rel)
		}
		return nil
	})
	// What Control recorded for the attempt: every dispatch of this run with
	// its state, every usage observation and cost entry (the ledger, not the
	// Proxy's say-so).
	ledger := map[string]any{}
	for name, q := range map[string]string{
		"dispatches":         "SELECT row_to_json(d) FROM dispatches d WHERE d.call_id LIKE $1 ORDER BY d.call_id",
		"usage_observations": "SELECT row_to_json(u) FROM usage_observations u JOIN dispatches d ON d.dispatch_id = u.dispatch_id WHERE d.call_id LIKE $1 ORDER BY d.call_id, u.sequence",
		"cost_entries":       "SELECT row_to_json(c) FROM cost_entries c JOIN dispatches d ON d.dispatch_id = c.dispatch_id WHERE d.call_id LIKE $1 ORDER BY d.call_id",
	} {
		rows, err := conn.Query(ctx, q, "smoke-"+run+"%")
		require.NoError(t, err)
		var list []json.RawMessage
		for rows.Next() {
			var raw json.RawMessage
			require.NoError(t, rows.Scan(&raw))
			list = append(list, raw)
		}
		rows.Close()
		ledger[name] = list
		if name == "dispatches" {
			summary.ControlDispatchs = len(list)
		}
	}
	ledgerRaw, _ := json.MarshalIndent(ledger, "", "  ")
	require.NoError(t, os.WriteFile(filepath.Join(evidence, "control-ledger.json"), ledgerRaw, 0o600))
	// The Proxy's public records of the run's calls.
	var proxyRecords []json.RawMessage
	for _, c := range calls {
		req, _ := http.NewRequest("GET", proxyURL+"/api/v1/model-calls/"+c.CallID, nil)
		req.Header.Set("Authorization", "Bearer "+tokenSidecar)
		if resp, err := http.DefaultClient.Do(req); err == nil {
			b, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 200 {
				proxyRecords = append(proxyRecords, b)
			}
		}
	}
	summary.ProxyCalls = len(proxyRecords)
	recordsRaw, _ := json.MarshalIndent(proxyRecords, "", "  ")
	require.NoError(t, os.WriteFile(filepath.Join(evidence, "model-proxy-records.json"), recordsRaw, 0o600))

	succeeded := 0
	for _, c := range calls {
		if c.Final == "done:succeeded" || c.Final == "done:" {
			succeeded++
		}
	}
	entry := filepath.Join(generated, "src", "index.tsx")
	entryRaw, entryErr := os.ReadFile(entry)
	switch {
	case coderErr != nil || outcome.Ended != "completed":
		set(0, "FAIL", fmt.Sprintf("coder ended %q (exit error %v, failure %q, code %q, error %q) after %d relay call(s), %d succeeded; files: %v", outcome.Ended, coderErr, outcome.Failure, outcome.FailureCode, outcome.Error, len(calls), succeeded, files))
	case succeeded == 0:
		set(0, "FAIL", fmt.Sprintf("no relay call ended done:succeeded (%d call(s): %+v)", len(calls), calls))
	case entryErr != nil || !strings.Contains(string(entryRaw), "HelloCard"):
		set(0, "FAIL", fmt.Sprintf("the coder completed but src/index.tsx is missing or does not define HelloCard; files: %v", files))
	case mode == "fixture":
		set(0, "DRY_RUN", fmt.Sprintf("fixture upstream (no model): %d relay call(s) through the Model Proxy, %d ended done:succeeded, Control recorded %d dispatch(es); the harness works, nothing was generated", len(calls), succeeded, summary.ControlDispatchs))
	default:
		set(0, "PASS", fmt.Sprintf("%s %s/%s: %d relay call(s) through the Model Proxy, %d ended done:succeeded, Control recorded %d dispatch(es); generated files: %v", mode, provider, model, len(calls), succeeded, summary.ControlDispatchs, files))
	}
	if summary.Checks[0].Status == "FAIL" {
		t.Fatalf("check 1 failed: %s", summary.Checks[0].Detail)
	}

	// Check 2: the trusted validator's build of exactly the generated source
	// (its profiles validator-dev-v1 / build-support-dev-v1 / host-abi-dev-v1,
	// unchanged), as the team's adapter invokes it.
	valOut := filepath.Join(scratch, "validator")
	valLog, err := os.OpenFile(filepath.Join(evidence, "validator.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	require.NoError(t, err)
	val := exec.CommandContext(ctx, "node", filepath.Join(validatorPkg, "dist", "cli.js"), sourceDir, "--source-revision", "1",
		"--component-id", "cmp_hello_card_smoke", "--puck-type", "HelloCard", "--package-name", "@anvilkit/hello-card-smoke",
		"--out", valOut, "--validator-profile", "validator-dev-v1")
	val.Dir = validatorPkg
	// HOME stays the caller's: Playwright's default browsers path is under it.
	val.Env = []string{"PATH=" + os.Getenv("PATH"), "HOME=" + os.Getenv("HOME"), "NODE_ENV=production", "ANVILKIT_VALIDATOR_CONTRACTS_DIR=" + filepath.Join(root, "contracts")}
	if p := os.Getenv("PLAYWRIGHT_BROWSERS_PATH"); p != "" {
		val.Env = append(val.Env, "PLAYWRIGHT_BROWSERS_PATH="+p)
	}
	val.Stdout, val.Stderr = valLog, valLog
	valErr := val.Run()
	valLog.Close()
	var cert struct {
		Verdict     string `json:"verdict"`
		FailureCode string `json:"failureCode"`
		Complete    bool   `json:"complete"`
		Checks      []struct {
			Name, Status, FailureCode string
		} `json:"checks"`
		Steps map[string]json.RawMessage `json:"steps"`
	}
	certRaw, certErr := os.ReadFile(filepath.Join(valOut, "certification.json"))
	if certErr == nil {
		_ = os.WriteFile(filepath.Join(evidence, "certification.json"), certRaw, 0o600)
		_ = json.Unmarshal(certRaw, &cert)
	}
	artifacts := filepath.Join(valOut, "artifacts")
	builtIndex := filepath.Join(artifacts, "index.js")
	if _, err := os.Stat(artifacts); err == nil {
		out, err := exec.Command("cp", "-a", artifacts, filepath.Join(evidence, "build-artifacts")).CombinedOutput()
		require.NoError(t, err, string(out))
	}
	buildStatus := ""
	for _, c := range cert.Checks {
		if c.Name == "build" {
			buildStatus = c.Status
		}
	}
	switch {
	case valErr != nil:
		set(1, "FAIL", fmt.Sprintf("validator exit %v (see validator.log); verdict %q code %q", valErr, cert.Verdict, cert.FailureCode))
	case certErr != nil:
		set(1, "FAIL", "the validator completed but wrote no certification.json")
	case fileMissing(builtIndex):
		set(1, "FAIL", fmt.Sprintf("no built artifacts/index.js; verdict %q code %q build check %q", cert.Verdict, cert.FailureCode, buildStatus))
	default:
		set(1, "PASS", fmt.Sprintf("built artifacts/index.js under the trusted profiles; certification verdict %q (code %q, complete %v, build check %q) — the verdict is diagnostic for this smoke, not a certification claim", cert.Verdict, cert.FailureCode, cert.Complete, buildStatus))
	}
	if summary.Checks[1].Status != "PASS" {
		t.Fatalf("check 2 failed: %s", summary.Checks[1].Detail)
	}

	// Check 3: the built artifact in a browser (the validator's own
	// Playwright and host bundles), the title visible, one screenshot.
	shot := filepath.Join(evidence, "screenshot.png")
	screenshot := exec.CommandContext(ctx, "node", filepath.Join(root, "tools", "smoke", "hello-card-screenshot.mjs"),
		"--validator", validatorPkg, "--artifacts", artifacts, "--component-json", filepath.Join(sourceDir, "component.json"), "--title", title, "--out", shot)
	screenshot.Env = append(os.Environ(), "HOME="+os.Getenv("HOME"))
	shotOut, shotErr := screenshot.CombinedOutput()
	_ = os.WriteFile(filepath.Join(evidence, "screenshot.log"), shotOut, 0o600)
	var shotReport struct {
		Visible bool   `json:"visible"`
		Text    string `json:"text"`
		Error   string `json:"error"`
	}
	if lines := strings.Split(strings.TrimSpace(string(shotOut)), "\n"); len(lines) > 0 {
		_ = json.Unmarshal([]byte(lines[len(lines)-1]), &shotReport)
	}
	switch {
	case shotErr != nil:
		set(2, "FAIL", fmt.Sprintf("screenshot script failed: %v: %s", shotErr, strings.TrimSpace(string(shotOut))))
	case !shotReport.Visible:
		set(2, "FAIL", fmt.Sprintf("the title %q is not visible in the rendered page (%s)", title, shotReport.Error))
	case fileMissing(shot):
		set(2, "FAIL", "no screenshot was written")
	default:
		set(2, "PASS", fmt.Sprintf("the built module rendered in Chromium headless shell with the title %q visible; screenshot %s", title, shot))
	}
	if summary.Checks[2].Status != "PASS" {
		t.Fatalf("check 3 failed: %s", summary.Checks[2].Detail)
	}
	t.Logf("smoke PASS; evidence under %s", evidence)
}

func fileMissing(path string) bool {
	st, err := os.Stat(path)
	return err != nil || st.Size() == 0
}

// scriptFixtureCoder scripts the countable upstream double for the dry
// run: one tool call per answer writing a HelloCard source (a fixed text,
// clearly not a generation), then the completion sentence. It only
// exercises the harness; the fixture mode never claims check 1.
func scriptFixtureCoder(t *testing.T, up *modelUpstream, title string) {
	t.Helper()
	files := [][2]string{
		{"component.json", `{"schemaVersion":1,"componentId":"cmp_hello_card_smoke","puckType":"HelloCard","entry":"src/index.tsx","styles":["styles/hello-card.css"],"resources":[],"usage":"README.md","editableFields":[{"name":"title","type":"text","default":"` + title + `"}]}` + "\n"},
		{"package.json", `{"name":"@anvilkit/hello-card-smoke","version":"1.0.0","description":"Fixture HelloCard of the smoke dry run.","license":"MIT","dependencies":{"@puckeditor/core":"0.23.0","react":"19.3.0"}}` + "\n"},
		{"pnpm-lock.yaml", "lockfileVersion: '9.0'\n\nsettings:\n  autoInstallPeers: true\n  excludeLinksFromLockfile: false\n\nimporters:\n\n  .:\n    dependencies:\n      '@puckeditor/core':\n        specifier: 0.23.0\n        version: 0.23.0\n      react:\n        specifier: 19.3.0\n        version: 19.3.0\n"},
		{"README.md", "# HelloCard\n\nThe default export is the Puck ComponentConfig; load styles/hello-card.css before importing the module.\n"},
		{"src/hello-card.tsx", "export interface HelloCardProps {\n\ttitle: string;\n}\n\nexport function HelloCard({ title }: HelloCardProps) {\n\treturn (\n\t\t<section className=\"ak-hello-card\" data-anvilkit-component=\"HelloCard\">\n\t\t\t<h2 className=\"ak-hello-card__title\">{title}</h2>\n\t\t</section>\n\t);\n}\n"},
		{"src/index.tsx", "import type { ComponentConfig } from \"@puckeditor/core\";\nimport { HelloCard, type HelloCardProps } from \"./hello-card\";\n\nexport const config: ComponentConfig<HelloCardProps> = {\n\tlabel: \"HelloCard\",\n\tfields: { title: { type: \"text\" } },\n\tdefaultProps: { title: \"" + title + "\" },\n\trender: ({ title }) => <HelloCard title={title} />,\n};\n\nexport { HelloCard };\nexport type { HelloCardProps };\nexport default config;\n"},
		{"styles/hello-card.css", ".ak-hello-card {\n\tbackground: #f3f6fb;\n\tborder: 1px solid #c7d2e3;\n\tborder-radius: 8px;\n\tpadding: 24px;\n}\n\n.ak-hello-card__title {\n\tmargin: 0;\n\tfont-size: 1.5rem;\n}\n"},
	}
	for i, f := range files {
		args, _ := json.Marshal(map[string]string{"path": f[0], "content": f[1]})
		up.next(upstreamBehavior{kind: "stream", text: []string{"Writing " + f[0] + "."}, tool: []string{fmt.Sprintf("tc_%d", i+1), "write", string(args)}})
	}
	up.next(upstreamBehavior{kind: "stream", text: []string{"The source is complete."}})
}
