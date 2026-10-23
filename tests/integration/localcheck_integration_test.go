//go:build integration

// Package integration proves the P05 LocalCheck control chain on the real
// development foundation (deploy/dev): PostgreSQL 17, Temporal 1.31.2 and a
// kind cluster. It builds and runs the three replacement binaries as
// separate processes and drives them only through the public API, the
// Control database (read-only assertions) and the Kubernetes API.
//
//	. .local/dev/env.sh && go test -tags integration -count=1 ./...
package integration

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/stretchr/testify/require"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	"go.yaml.in/yaml/v3"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"
)

const subjectDigest = "sha256:0dc7fa9db7237a2b5c96f70f59bb00f73bb86a0ca5554e91c312f9ada26e18b3"

// tokenA and tokenB are the bearer tokens of tenant_a/proj_a/user_a and
// tenant_b/proj_b/user_b: real tokens the development OIDC provider issues
// (P0.3; deploy/dev/oidc/config.json), verified by the API the harness
// starts under auth.mode oidc. An API running elsewhere
// (ANVILKIT_INTEGRATION_API_URL) verifies its own issuer, so its tokens are
// taken from ANVILKIT_INTEGRATION_TOKEN_A and ANVILKIT_INTEGRATION_TOKEN_B.
var tokenA, tokenB string

// oidcToken obtains a development user's access token from the development
// OIDC provider by password grant (the provider authenticates nobody; it
// maps the user name to the claims of deploy/dev/oidc/config.json).
func oidcToken(t testing.TB, user string) string {
	t.Helper()
	issuer := os.Getenv("ANVILKIT_DEV_OIDC_ISSUER")
	require.NotEmpty(t, issuer, "ANVILKIT_DEV_OIDC_ISSUER not set; source .local/dev/env.sh after deploy/dev/up.sh")
	resp, err := http.PostForm(issuer+"/token", url.Values{"grant_type": {"password"}, "username": {user}, "password": {"development"},
		"client_id": {"anvilkit-agent-api"}, "scope": {"openid"}})
	require.NoError(t, err)
	defer resp.Body.Close()
	var out struct {
		AccessToken string `json:"access_token"`
	}
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.NoError(t, json.NewDecoder(resp.Body).Decode(&out))
	require.NotEmpty(t, out.AccessToken)
	return out.AccessToken
}

type env struct {
	root, bin, local string
	controlDSN       string
	temporal         string
	kubeconfig       string
	controlAddr      string
	// controlAddrB is a second Control process sharing the database and the
	// inventory directory; when set, the Workflow talks to it while the API
	// keeps talking to controlAddr.
	controlAddrB string
	apiURL       string
	// workflowConfig is the worker's configuration file; the default is the
	// service's checked-in config.yaml, fault scenarios supply their own.
	workflowConfig string
	// controlConfig is the Control processes' configuration file; the
	// default is the checked-in config.yaml, the dispatch scenarios supply
	// a copy with the DEVELOPMENT_ONLY fixtures enabled.
	controlConfig string
	// withoutWorkflow leaves the Workflow worker (and the API) out: the
	// dispatch scenarios drive Control directly over gRPC.
	withoutWorkflow bool
	// externalAPI is set when the API under test runs outside the harness.
	externalAPI string
	// controlEnv is additional environment of the Control processes (a
	// placement such as the Model Proxy address of the P11 scenarios).
	controlEnv []string
	// workflowEnv is additional environment of the Workflow worker (the
	// Model Proxy placement and the Pagix double directory of the P13
	// scenarios).
	workflowEnv []string
	procs       map[string]*exec.Cmd
	// health is each Control process's plaintext probe listener (/readyz).
	health map[string]string
	// binaries names another binary for a process (a rolled Control,
	// TEST-05); the default is the harness's build.
	binaries map[string]string
	t        *testing.T
}

func repoRoot(t *testing.T) string {
	wd, err := os.Getwd()
	require.NoError(t, err)
	return filepath.Clean(filepath.Join(wd, "..", ".."))
}

var integrationPort atomic.Uint32

// Keep listener reservations outside Linux's ephemeral client-port range.
// The OS must still confirm availability; already owned listeners are skipped.
func freePort(t *testing.T) string {
	t.Helper()
	for n := 0; n < 10000; n++ {
		port := fmt.Sprint(20000 + integrationPort.Add(1)%10000)
		listener, err := net.Listen("tcp", "127.0.0.1:"+port)
		if err == nil {
			listener.Close()
			return port
		}
	}
	t.Fatal("no free integration listener port")
	return ""
}

// setup builds the three binaries and starts one Control, the API and the
// Workflow worker; setupTwoControls starts a second Control process first.
func setup(t *testing.T) *env {
	return setupWith(t, false)
}

func setupTwoControls(t *testing.T) *env {
	return setupWith(t, true)
}

func setupWith(t *testing.T, twoControls bool) *env {
	return setupEnv(t, twoControls, "")
}

func setupEnv(t *testing.T, twoControls bool, workflowConfig string) *env {
	return setupProcesses(t, twoControls, workflowConfig, "", false)
}

// serviceDirs are the module directories of the binaries the harness builds:
// the three service repositories mounted at services/agent/{control,api,
// workflow}, each built from its own module (the parent prepares the
// cross-service processes; no service builds another).
var serviceDirs = map[string]string{
	"anvilkit-agent-control":  filepath.Join("services", "agent", "control"),
	"anvilkit-agent-api":      filepath.Join("services", "agent", "api"),
	"anvilkit-agent-workflow": filepath.Join("services", "agent", "workflow"),
}

func setupProcesses(t *testing.T, twoControls bool, workflowConfig, controlConfig string, withoutWorkflow bool) *env {
	return setupProcessesEnv(t, twoControls, workflowConfig, controlConfig, withoutWorkflow, nil)
}

// setupProcessesEnv is setupProcesses with additional environment for the
// Control processes (a placement such as the Model Proxy address, P11).
func setupProcessesEnv(t *testing.T, twoControls bool, workflowConfig, controlConfig string, withoutWorkflow bool, controlEnv []string) *env {
	return setupProcessesFull(t, twoControls, workflowConfig, controlConfig, withoutWorkflow, controlEnv, nil)
}

// setupProcessesFull is setupProcessesEnv with additional environment for
// the Workflow worker too (the P13 scenarios' Model Proxy placement and
// Pagix double directory).
func setupProcessesFull(t *testing.T, twoControls bool, workflowConfig, controlConfig string, withoutWorkflow bool, controlEnv, workflowEnv []string) *env {
	for _, v := range []string{"ANVILKIT_DEV_CONTROL_DSN", "ANVILKIT_DEV_TEMPORAL_ADDRESS", "KUBECONFIG"} {
		if os.Getenv(v) == "" {
			skipMissing(t, v, "not set; source .local/dev/env.sh after deploy/dev/up.sh")
		}
	}
	e := &env{root: repoRoot(t), bin: t.TempDir(), local: logDir(t), controlDSN: os.Getenv("ANVILKIT_DEV_CONTROL_DSN"),
		temporal: os.Getenv("ANVILKIT_DEV_TEMPORAL_ADDRESS"), kubeconfig: os.Getenv("KUBECONFIG"), workflowConfig: workflowConfig, controlConfig: controlConfig,
		withoutWorkflow: withoutWorkflow, controlEnv: controlEnv, workflowEnv: workflowEnv, procs: map[string]*exec.Cmd{}, health: map[string]string{}, binaries: map[string]string{}, t: t}
	if e.workflowConfig == "" {
		e.workflowConfig = filepath.Join(e.root, serviceDirs["anvilkit-agent-workflow"], "config.yaml")
	}
	if e.controlConfig == "" {
		e.controlConfig = filepath.Join(e.root, serviceDirs["anvilkit-agent-control"], "config.yaml")
	}
	// Each harness owns its two queues. Concurrent development workers may
	// keep polling their queues; none can consume this harness's Activities.
	queue := fmt.Sprintf("anvilkit-integration-%d-%d", os.Getpid(), time.Now().UnixNano())
	for _, item := range []struct {
		src    *string
		name   string
		worker bool
	}{
		{&e.controlConfig, "control.yaml", false}, {&e.workflowConfig, "workflow.yaml", true},
	} {
		raw, err := os.ReadFile(*item.src)
		require.NoError(t, err)
		var cfg map[string]any
		require.NoError(t, yaml.Unmarshal(raw, &cfg))
		temporal, ok := cfg["temporal"].(map[string]any)
		require.True(t, ok, "configuration declares Temporal settings")
		temporal["task_queue"] = queue
		if item.worker {
			temporal["control_task_queue"] = queue + "-control"
			if namespace := os.Getenv("ANVILKIT_INTEGRATION_JOB_NAMESPACE"); namespace != "" {
				cfg["kubernetes"].(map[string]any)["namespace"] = namespace
			}
		}
		raw, err = yaml.Marshal(cfg)
		require.NoError(t, err)
		*item.src = filepath.Join(e.local, item.name)
		require.NoError(t, os.WriteFile(*item.src, raw, 0o600))
	}
	// ANVILKIT_INTEGRATION_API_URL names an API that is already running
	// elsewhere (for example the chart installed in the development cluster):
	// the harness then builds and starts no API of its own and, so that the
	// external API can reach it, listens for Control on
	// ANVILKIT_INTEGRATION_CONTROL_LISTEN (a non-loopback placement such as
	// 0.0.0.0:9101) instead of a free loopback port.
	e.externalAPI = os.Getenv("ANVILKIT_INTEGRATION_API_URL")
	for _, svc := range []string{"anvilkit-agent-control", "anvilkit-agent-api", "anvilkit-agent-workflow"} {
		if svc == "anvilkit-agent-api" && e.externalAPI != "" {
			continue
		}
		// ANVILKIT_INTEGRATION_CONTROL_BINARY names a prebuilt Control (an
		// earlier image, TEST-05) the harness runs instead of building one.
		if prebuilt := os.Getenv("ANVILKIT_INTEGRATION_CONTROL_BINARY"); svc == "anvilkit-agent-control" && prebuilt != "" {
			raw, err := os.ReadFile(prebuilt)
			require.NoError(t, err)
			require.NoError(t, os.WriteFile(filepath.Join(e.bin, svc), raw, 0o755))
			continue
		}
		cmd := exec.Command("go", "build", "-o", filepath.Join(e.bin, svc), "./cmd/"+svc)
		cmd.Dir = filepath.Join(e.root, serviceDirs[svc])
		out, err := cmd.CombinedOutput()
		require.NoError(t, err, string(out))
	}
	if e.externalAPI != "" {
		tokenA, tokenB = os.Getenv("ANVILKIT_INTEGRATION_TOKEN_A"), os.Getenv("ANVILKIT_INTEGRATION_TOKEN_B")
	} else {
		tokenA, tokenB = oidcToken(t, "user-a"), oidcToken(t, "user-b")
	}
	e.controlAddr = "127.0.0.1:" + freePort(t)
	if listen := os.Getenv("ANVILKIT_INTEGRATION_CONTROL_LISTEN"); listen != "" && e.externalAPI != "" {
		e.controlAddr = listen
	}
	// ANVILKIT_INTEGRATION_JOB_CONTROL_LISTEN places Control where the access
	// sidecar of a kind Job Pod reaches it (for example 0.0.0.0:<port>, dialed
	// through the kind gateway); the scenario that sets it also names that
	// address to the Workflow (ANVILKIT_WORKFLOW_SIDECAR_CONTROL_ADDRESS).
	if listen := os.Getenv("ANVILKIT_INTEGRATION_JOB_CONTROL_LISTEN"); listen != "" {
		e.controlAddr = listen
	}
	e.apiURL = "http://127.0.0.1:" + freePort(t)
	if e.externalAPI != "" {
		e.apiURL = e.externalAPI
	}
	// Registered before the first process starts: a readiness wait that fails
	// (an API that is not reachable, a port that is not forwarded) must not
	// leave a Control listening on the shared port with a deleted inventory.
	t.Cleanup(func() {
		for _, name := range []string{"workflow", "api", "control-b", "control"} {
			e.stop(name)
		}
	})
	e.start("control")
	e.waitTCP(e.controlAddr)
	if twoControls {
		e.controlAddrB = "127.0.0.1:" + freePort(t)
		e.start("control-b")
		e.waitTCP(e.controlAddrB)
	}
	if !e.withoutWorkflow {
		if e.externalAPI == "" {
			e.start("api")
		}
		e.waitHTTP(e.apiURL + "/healthz")
		e.start("workflow")
	}
	return e
}

func (e *env) start(name string) {
	var cmd *exec.Cmd
	switch name {
	case "control", "control-b":
		listen := e.controlAddr
		if name == "control-b" {
			listen = e.controlAddrB
		}
		e.health[name] = "127.0.0.1:" + freePort(e.t)
		bin := filepath.Join(e.bin, "anvilkit-agent-control")
		if b := e.binaries[name]; b != "" {
			bin = b
		}
		cmd = exec.Command(bin)
		cmd.Env = append(os.Environ(),
			"ANVILKIT_CONTROL_CONFIG="+e.controlConfig,
			"ANVILKIT_CONTROL_LISTEN="+listen, "ANVILKIT_CONTROL_DATABASE_URL="+e.controlDSN,
			"ANVILKIT_CONTROL_INVENTORY_DIR="+filepath.Join(e.local, "inventory"), "ANVILKIT_CONTROL_TEMPORAL_ADDRESS="+e.temporal,
			// P0.1: each Control process has its own plaintext probe listener.
			"ANVILKIT_CONTROL_HEALTH_LISTEN="+e.health[name])
		cmd.Env = append(cmd.Env, e.controlEnv...)
	case "api":
		cmd = exec.Command(filepath.Join(e.bin, "anvilkit-agent-api"))
		cmd.Env = append(os.Environ(),
			"ANVILKIT_API_CONFIG="+filepath.Join(e.root, serviceDirs["anvilkit-agent-api"], "config.yaml"),
			"ANVILKIT_API_LISTEN="+strings.TrimPrefix(e.apiURL, "http://"), "ANVILKIT_API_CONTROL_ADDRESS="+e.controlAddr,
			// P0.3: real tokens of the development OIDC provider.
			"ANVILKIT_API_AUTH_MODE=oidc", "ANVILKIT_API_AUTH_OIDC_ISSUER="+os.Getenv("ANVILKIT_DEV_OIDC_ISSUER"))
	case "workflow":
		controlAddr := e.controlAddr
		if e.controlAddrB != "" {
			controlAddr = e.controlAddrB
		}
		backend := os.Getenv("ANVILKIT_INTEGRATION_LAUNCH_BACKEND")
		if backend == "" {
			backend = "kind-anvilkit-dev"
		}
		cmd = exec.Command(filepath.Join(e.bin, "anvilkit-agent-workflow"))
		cmd.Env = append(os.Environ(),
			"ANVILKIT_WORKFLOW_CONFIG="+e.workflowConfig,
			"ANVILKIT_WORKFLOW_TEMPORAL_ADDRESS="+e.temporal, "ANVILKIT_WORKFLOW_CONTROL_ADDRESS="+controlAddr,
			"ANVILKIT_WORKFLOW_KUBECONFIG="+e.kubeconfig, "ANVILKIT_WORKFLOW_LAUNCH_BACKEND="+backend,
			"ANVILKIT_WORKFLOW_BUILD_ID=integration-"+fmt.Sprint(time.Now().Unix()),
			// The worker's health listener on a free loopback port, so a
			// restarted or concurrently started worker never collides.
			"ANVILKIT_WORKFLOW_HEALTH_LISTEN=127.0.0.1:"+freePort(e.t))
		cmd.Env = append(cmd.Env, e.workflowEnv...)
	}
	logf, err := os.OpenFile(filepath.Join(e.local, name+".log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	require.NoError(e.t, err)
	cmd.Stdout, cmd.Stderr = logf, logf
	require.NoError(e.t, cmd.Start(), name)
	e.procs[name] = cmd
}

func (e *env) stop(name string) {
	cmd := e.procs[name]
	if cmd == nil || cmd.Process == nil {
		return
	}
	_ = cmd.Process.Signal(syscall.SIGTERM)
	done := make(chan struct{})
	go func() { _, _ = cmd.Process.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(30 * time.Second):
		_ = cmd.Process.Kill()
	}
	delete(e.procs, name)
}

func (e *env) kill(name string) {
	cmd := e.procs[name]
	require.NotNil(e.t, cmd)
	require.NoError(e.t, cmd.Process.Kill())
	_, _ = cmd.Process.Wait()
	delete(e.procs, name)
}

func (e *env) waitTCP(addr string) {
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if c, err := net.DialTimeout("tcp", addr, time.Second); err == nil {
			c.Close()
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	e.t.Fatalf("%s did not open", addr)
}

func (e *env) waitHTTP(url string) {
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if resp, err := http.Get(url); err == nil {
			resp.Body.Close()
			return
		}
		time.Sleep(200 * time.Millisecond)
	}
	e.t.Fatalf("%s not ready", url)
}

func (e *env) call(method, path, token, body string) (int, map[string]any) {
	req, err := http.NewRequest(method, e.apiURL+path, strings.NewReader(body))
	require.NoError(e.t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	require.NoError(e.t, err)
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

type frame struct {
	ID, Event string
	Data      map[string]any
}

// stream reads SSE frames until stop returns true or the connection ends.
func (e *env) stream(ctx context.Context, operationID, token, lastEventID string, stop func(frame) bool) []frame {
	req, err := http.NewRequestWithContext(ctx, "GET", e.apiURL+"/api/v1/operations/"+operationID+"/events", nil)
	require.NoError(e.t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	if lastEventID != "" {
		req.Header.Set("Last-Event-ID", lastEventID)
	}
	resp, err := http.DefaultClient.Do(req)
	require.NoError(e.t, err)
	defer resp.Body.Close()
	require.Equal(e.t, 200, resp.StatusCode)
	var frames []frame
	cur := frame{}
	reader := bufio.NewReader(resp.Body)
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return frames
		}
		line = strings.TrimRight(line, "\n")
		if line == "" {
			if cur.Event != "" {
				frames = append(frames, cur)
				if stop(cur) {
					return frames
				}
				cur = frame{}
			}
			continue
		}
		if strings.HasPrefix(line, ":") {
			continue
		}
		// SSE field rule (and gin-contrib/sse's decoder): the value after the
		// first colon, minus one optional leading space.
		k, v, _ := strings.Cut(line, ":")
		v = strings.TrimPrefix(v, " ")
		switch k {
		case "id":
			cur.ID = v
		case "event":
			cur.Event = v
		case "data":
			_ = json.Unmarshal([]byte(v), &cur.Data)
		}
	}
}

func lifecycle(f frame) string {
	p, _ := f.Data["payload"].(map[string]any)
	l, _ := p["lifecycle"].(string)
	return l
}

func phase(f frame) string {
	p, _ := f.Data["payload"].(map[string]any)
	l, _ := p["phase"].(string)
	return l
}

func terminal(f frame) bool {
	switch lifecycle(f) {
	case "succeeded", "failed", "canceled":
		return true
	}
	return false
}

// final reports the operation's last event: terminal, and its money no
// longer awaiting settlement (P2.1). Control closes the stream after it.
func final(f frame) bool {
	p, _ := f.Data["payload"].(map[string]any)
	finance, _ := p["finance"].(string)
	return terminal(f) && finance != "funded" && finance != "not_funded"
}

func createBody(commandID string) string {
	return `{"commandId":"` + commandID + `","kind":"local_check","subject":{"profileId":"local-check-v1","subjectDigest":"` + subjectDigest + `"}}`
}

func (e *env) k8s() kubernetes.Interface {
	cfg, err := clientcmd.BuildConfigFromFlags("", e.kubeconfig)
	require.NoError(e.t, err)
	cs, err := kubernetes.NewForConfig(cfg)
	require.NoError(e.t, err)
	return cs
}

func (e *env) db() *pgx.Conn {
	conn, err := pgx.Connect(context.Background(), e.controlDSN)
	require.NoError(e.t, err)
	e.t.Cleanup(func() { conn.Close(context.Background()) })
	return conn
}

func launchKey(operationID string) string {
	return "lc-" + strings.ToLower(strings.TrimPrefix(operationID, "op_"))
}

func (e *env) assertSingleAcceptedResult(operationID string) {
	conn := e.db()
	var stages, attempts, instances int
	var verdict string
	require.NoError(e.t, conn.QueryRow(context.Background(), "SELECT count(*) FROM stage_manifests WHERE operation_id = $1", operationID).Scan(&stages))
	require.NoError(e.t, conn.QueryRow(context.Background(), "SELECT count(*) FROM attempts WHERE operation_id = $1", operationID).Scan(&attempts))
	require.NoError(e.t, conn.QueryRow(context.Background(), "SELECT count(*) FROM physical_instances i JOIN attempts a ON a.attempt_id = i.attempt_id WHERE a.operation_id = $1", operationID).Scan(&instances))
	require.NoError(e.t, conn.QueryRow(context.Background(), "SELECT verdict FROM stage_manifests WHERE operation_id = $1", operationID).Scan(&verdict))
	require.Equal(e.t, 1, stages, "exactly one accepted result")
	require.Equal(e.t, 1, attempts, "one attempt")
	require.GreaterOrEqual(e.t, instances, 1, "the real Pod was registered")
	require.Equal(e.t, "certified", verdict)
}

// assertJobGone asserts that the Job and every Pod of the launch are gone
// from the API server now, without polling: the terminal event, cleanup
// complete and cancel applied are only reported after the launcher observed
// that, so any lingering Job or Pod here is a defect.
func (e *env) assertJobGone(operationID string) {
	cs := e.k8s()
	_, err := cs.BatchV1().Jobs("anvilkit-components").Get(context.Background(), launchKey(operationID), metav1.GetOptions{})
	require.True(e.t, apierrors.IsNotFound(err), "job %s must be gone when cleanup is reported complete: %v", launchKey(operationID), err)
	pods, err := cs.CoreV1().Pods("anvilkit-components").List(context.Background(), metav1.ListOptions{LabelSelector: "anvilkit.io/launch-key=" + launchKey(operationID)})
	require.NoError(e.t, err)
	require.Empty(e.t, pods.Items, "no Pod of %s may remain when cleanup is reported complete", launchKey(operationID))
}

func TestLocalCheckChain(t *testing.T) {
	e := setup(t)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
	defer cancel()

	t.Run("submit, observe over SSE, one accepted result, cleanup", func(t *testing.T) {
		status, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-1"))
		require.Equal(t, 202, status, "%v", out)
		opID := out["operationId"].(string)
		require.Equal(t, "accepted", out["lifecycle"])

		frames := e.stream(ctx, opID, tokenA, "", final)
		require.NotEmpty(t, frames)
		last := frames[len(frames)-1]
		require.Equal(t, "succeeded", lifecycle(last), "frames: %v", phases(frames))
		// The terminal event is followed by the financial settlement, the
		// operation's last event; the stream ends only after it.
		require.Equal(t, "finance:settled", last.Data["transitionId"], "frames: %v", phases(frames))
		require.Equal(t, []string{"intake", "accepted", "scheduled", "attempt_open", "launch_prepared", "running", "result_accepted", "closed"}, phases(frames[:len(frames)-1]))
		// Plan 0005 Phase 2: the attempt transitions carry the attempt's step,
		// visit and ordinal, the accepted result its verdict; the intake
		// transitions carry none of them.
		for _, f := range frames {
			payload, _ := f.Data["payload"].(map[string]any)
			switch payload["phase"] {
			case "attempt_open", "launch_prepared", "running", "result_accepted", "closed":
				require.Equal(t, "local-check", payload["step"], "frame %v", f.ID)
				require.Equal(t, "0", payload["visit"], "frame %v", f.ID)
				require.Equal(t, "1", payload["attemptOrdinal"], "frame %v", f.ID)
			default:
				require.NotContains(t, payload, "step", "frame %v", f.ID)
			}
			if payload["phase"] == "result_accepted" {
				require.Equal(t, "certified", payload["verdict"], "frame %v", f.ID)
			} else {
				require.NotContains(t, payload, "verdict", "frame %v", f.ID)
			}
		}
		for i := 1; i < len(frames); i++ {
			require.Less(t, frames[i-1].ID, frames[i].ID, "event ids are strictly increasing")
		}

		status, view := e.call("GET", "/api/v1/operations/"+opID, tokenA, "")
		require.Equal(t, 200, status)
		require.Equal(t, "succeeded", view["lifecycle"])
		require.Equal(t, "complete", view["cleanup"])
		require.Equal(t, "settled", view["finance"])
		require.Equal(t, last.ID, view["coveredEventSeq"])
		e.assertSingleAcceptedResult(opID)
		e.assertJobGone(opID)

		// Cross-tenant: not found, no disclosure.
		status, _ = e.call("GET", "/api/v1/operations/"+opID, tokenB, "")
		require.Equal(t, 404, status)

		// Reconnect after the second event: only later events are replayed.
		replay := e.stream(ctx, opID, tokenA, "2", terminal)
		require.Equal(t, "3", replay[0].ID)
		require.Equal(t, "succeeded", lifecycle(replay[len(replay)-1]))
		// A cursor beyond the covered sequence yields a reset frame.
		reset := e.stream(ctx, opID, tokenA, "999", func(f frame) bool { return f.Event == "reset_required" })
		require.Equal(t, "reset_required", reset[len(reset)-1].Event)
		require.Equal(t, last.ID, reset[len(reset)-1].Data["coveredEventSeq"])
	})

	t.Run("duplicate submission returns the original operation and starts nothing new", func(t *testing.T) {
		status, first := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-dup"))
		require.Equal(t, 202, status, "%v", first)
		status, second := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-dup"))
		require.Equal(t, 202, status)
		require.Equal(t, first["operationId"], second["operationId"])
		status, conflict := e.call("POST", "/api/v1/operations", tokenA, `{"commandId":"cmd-`+run+`-dup","kind":"local_check","subject":{"profileId":"local-check-v1","subjectDigest":"sha256:1111111111111111111111111111111111111111111111111111111111111111"}}`)
		require.Equal(t, 409, status)
		require.Equal(t, "IDEMPOTENCY_CONFLICT", conflict["error"].(map[string]any)["code"])
		opID := first["operationId"].(string)
		frames := e.stream(ctx, opID, tokenA, "", terminal)
		require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))
		var ops int
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT count(*) FROM operations WHERE command_id = $1", "cmd-"+run+"-dup").Scan(&ops))
		require.Equal(t, 1, ops)
		e.assertSingleAcceptedResult(opID)
	})

	t.Run("cancel while the Job runs fences new launches and ends canceled with cleanup", func(t *testing.T) {
		status, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-cancel"))
		require.Equal(t, 202, status, "%v", out)
		opID := out["operationId"].(string)
		var revision string
		// The cancel waits for the running Job: a cancel between the launch
		// record and its confirmation is refused confirmation (P1.5) and no
		// Job is ever created, which is the not_required cleanup, not this case.
		e.stream(ctx, opID, tokenA, "", func(f frame) bool {
			if phase(f) == "running" {
				revision, _ = f.Data["revision"].(string)
				return true
			}
			return terminal(f)
		})
		require.NotEmpty(t, revision, "operation reached the running Job")
		status, receipt := e.call("POST", "/api/v1/operations/"+opID+"/commands", tokenA, `{"commandId":"cmd-`+run+`-cancel-1","kind":"cancel","expectedRevision":"`+revision+`"}`)
		if status == 409 {
			// The projection moved between the frame and the command: read and retry once.
			_, view := e.call("GET", "/api/v1/operations/"+opID, tokenA, "")
			status, receipt = e.call("POST", "/api/v1/operations/"+opID+"/commands", tokenA, `{"commandId":"cmd-`+run+`-cancel-2","kind":"cancel","expectedRevision":"`+view["revision"].(string)+`"}`)
		}
		require.Equal(t, 202, status, "%v", receipt)
		require.Contains(t, []any{"pending", "applied"}, receipt["outcome"])
		frames := e.stream(ctx, opID, tokenA, "", terminal)
		require.Equal(t, "canceled", lifecycle(frames[len(frames)-1]), phases(frames))
		_, view := e.call("GET", "/api/v1/operations/"+opID, tokenA, "")
		require.Equal(t, "cancel_applied", view["control"])
		require.Equal(t, "complete", view["cleanup"])
		e.assertJobGone(opID)
		var outcome string
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT outcome FROM operation_commands WHERE operation_id = $1 AND kind = 'cancel' ORDER BY accepted_at DESC LIMIT 1", opID).Scan(&outcome))
		require.Equal(t, "applied", outcome)
	})

	t.Run("worker killed mid-observation recovers under the original identities", func(t *testing.T) {
		status, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-restart"))
		require.Equal(t, 202, status, "%v", out)
		opID := out["operationId"].(string)
		e.stream(ctx, opID, tokenA, "", func(f frame) bool { return phase(f) == "launch_prepared" || terminal(f) })
		// The fault is a worker gone while it observes the Job: it is
		// injected only once Temporal shows the run's ObserveJob Activity
		// started on a worker, which the run schedules after the create
		// request's answer is in its history. Killing right after
		// launch_prepared could instead hit the create request in flight,
		// which is the unresolved-create scenario (TestUnresolvedCreateRecovery),
		// not this one. The bound is the launch window the run itself has.
		require.Eventually(t, func() bool { return e.observationStarted(opID) }, 2*time.Minute, 250*time.Millisecond,
			"the run's ObserveJob Activity did not start on the worker")
		e.kill("workflow")
		time.Sleep(3 * time.Second)
		e.start("workflow")
		frames := e.stream(ctx, opID, tokenA, "", terminal)
		require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))
		e.assertSingleAcceptedResult(opID)
		var launches int
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT count(*) FROM launches WHERE operation_id = $1", opID).Scan(&launches))
		require.Equal(t, 1, launches, "the restarted worker reused the inventoried launch")
		e.assertJobGone(opID)
	})
}

// observationStarted reports whether the operation's Workflow run currently
// has its ObserveJob Activity started on a worker (Temporal's pending
// activity state, not the SSE projection): the evidence that observation of
// the launched Job is under way and the create request is answered.
func (e *env) observationStarted(operationID string) bool {
	tc, err := client.Dial(client.Options{HostPort: e.temporal, Namespace: "anvilkit"})
	require.NoError(e.t, err)
	defer tc.Close()
	desc, err := tc.DescribeWorkflowExecution(context.Background(), operationID, "")
	if err != nil {
		return false
	}
	for _, pa := range desc.GetPendingActivities() {
		if pa.GetActivityType().GetName() == "ObserveJob" && pa.GetState() == enumspb.PENDING_ACTIVITY_STATE_STARTED {
			return true
		}
	}
	return false
}

func phases(frames []frame) []string {
	out := make([]string, 0, len(frames))
	for _, f := range frames {
		out = append(out, phase(f))
	}
	return out
}

// logDir keeps a scenario's process logs under ANVILKIT_INTEGRATION_LOG_DIR
// when that is set (one directory per test, for reading after a failure),
// otherwise in a temporary directory.
func logDir(t *testing.T) string {
	t.Helper()
	base := os.Getenv("ANVILKIT_INTEGRATION_LOG_DIR")
	if base == "" {
		return t.TempDir()
	}
	dir := filepath.Join(base, strings.ReplaceAll(t.Name(), "/", "_")+"-"+fmt.Sprint(time.Now().Unix()))
	require.NoError(t, os.MkdirAll(dir, 0o700))
	return dir
}
