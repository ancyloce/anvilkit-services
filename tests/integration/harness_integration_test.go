//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	"github.com/ancyloce/anvilkit-agent-contracts/go/jobschema"
)

// supervisorRepo and teamRepo are the codegen Job's two repositories as the
// parent mounts them (submodules anvilkit-job-codegen-supervisor and
// anvilkit-job-codegen-team).
func supervisorRepo(root string) string { return filepath.Join(root, "jobs", "codegen", "supervisor") }
func teamRepo(root string) string       { return filepath.Join(root, "jobs", "codegen", "team") }

// harnessBinaries builds the access sidecar and the codegen supervisor and
// candidate from their modules into a world-traversable directory: the
// sidecar runs as UID 10002 and the candidate as UID 10001, and go test's
// own directories are 0700.
type harnessBinaries struct {
	dir, sidecar, supervisor, candidate string
}

func buildHarness(t *testing.T, root string) harnessBinaries {
	t.Helper()
	dir, err := os.MkdirTemp("/tmp", "anvilkit-harness-")
	require.NoError(t, err)
	t.Cleanup(func() { os.RemoveAll(dir) })
	require.NoError(t, os.Chmod(dir, 0o755))
	b := harnessBinaries{dir: dir, sidecar: filepath.Join(dir, "anvilkit-job-access-sidecar"), supervisor: filepath.Join(dir, "anvilkit-codegen-supervisor"), candidate: filepath.Join(dir, "anvilkit-codegen-candidate")}
	for out, spec := range map[string][2]string{
		b.sidecar:    {filepath.Join(root, "jobs", "shared", "access-sidecar"), "./cmd/anvilkit-job-access-sidecar"},
		b.supervisor: {supervisorRepo(root), "./cmd/anvilkit-codegen-supervisor"},
		b.candidate:  {supervisorRepo(root), "./cmd/anvilkit-codegen-candidate"},
	} {
		cmd := exec.Command("go", "build", "-o", out, spec[1])
		cmd.Dir = spec[0]
		raw, err := cmd.CombinedOutput()
		require.NoError(t, err, string(raw))
	}
	return b
}

// harnessLayout is the DD-03 mount layout on the host: root-owned
// emptyDir-like directories, the sidecar's socket volume, the protected
// fixtures and the explicit agent directory of the trusted resources.
type harnessLayout struct {
	root, workspace, verdict, run, sockets, agent, fixtures, termination, supervisorCfg, sidecarCfg string
}

func harnessDirs(t *testing.T, root string, b harnessBinaries, name string) harnessLayout {
	t.Helper()
	base := filepath.Join(b.dir, name)
	l := harnessLayout{root: base, workspace: filepath.Join(base, "workspace"), verdict: filepath.Join(base, "verdict"), run: filepath.Join(base, "run"), agent: filepath.Join(base, "agent"), fixtures: filepath.Join(base, "fixtures"), termination: filepath.Join(base, "termination-log"), supervisorCfg: filepath.Join(base, "codegen.yaml"), sidecarCfg: filepath.Join(base, "sidecar.yaml")}
	l.sockets = filepath.Join(l.run, "sockets")
	require.NoError(t, os.MkdirAll(base, 0o755))
	for _, d := range []string{l.workspace, l.verdict, l.run} {
		require.NoError(t, os.Mkdir(d, 0o777))
		require.NoError(t, os.Chmod(d, 0o777))
	}
	require.NoError(t, os.Mkdir(l.agent, 0o700))
	src, err := os.ReadFile(filepath.Join(supervisorRepo(root), "agent", "resources.json"))
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(filepath.Join(l.agent, "resources.json"), src, 0o600))
	require.NoError(t, os.Mkdir(l.fixtures, 0o700))
	fixture, err := os.ReadFile(filepath.Join(supervisorRepo(root), "fixtures", "fixed-input.txt"))
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(filepath.Join(l.fixtures, "fixed-input.txt"), fixture, 0o600))
	require.NoError(t, os.WriteFile(l.supervisorCfg, []byte(fmt.Sprintf("paths:\n  workspace: %s\n  verdict: %s\n  sockets: %s\n  agent_dir: %s\n  fixtures: %s\n  termination_log: %s\ncandidate:\n  program: %s\n  timeout: 120s\nsidecar:\n  wait: 30s\n", l.workspace, l.verdict, l.sockets, l.agent, l.fixtures, l.termination, b.candidate)), 0o600))
	require.NoError(t, os.WriteFile(l.sidecarCfg, sidecarConfig(t, base), 0o644))
	return l
}

// sidecarConfig is the host-run sidecar's configuration: mTLS to Control
// and MCP with a copy of its development certificate owned by the sidecar's
// UID (the Job template mounts it into that container only), the Model
// Proxy connection on the DEVELOPMENT_ONLY bearer of the harness's proxy,
// admitted by the file's own development guard.
func sidecarConfig(t *testing.T, dir string) []byte {
	t.Helper()
	certFile, keyFile, caFile := identityFiles(t, principalSidecar)
	id := filepath.Join(dir, "identity")
	require.NoError(t, os.MkdirAll(id, 0o700))
	for src, name := range map[string]string{certFile: "tls.crt", keyFile: "tls.key", caFile: "ca.crt"} {
		raw, err := os.ReadFile(src)
		require.NoError(t, err)
		require.NoError(t, os.WriteFile(filepath.Join(id, name), raw, 0o600))
		require.NoError(t, os.Chown(filepath.Join(id, name), 10002, 10002))
	}
	require.NoError(t, os.Chown(id, 10002, 10002))
	return []byte(fmt.Sprintf("development:\n  enabled: true\ncontrol:\n  timeout: 15s\nidentity:\n  mode: mtls\n  mtls:\n    cert_file: %s\n    key_file: %s\n    ca_file: %s\n    server_name: anvilkit-agent-control\nmodel_proxy:\n  identity_mode: development\nmcp:\n  server_name: anvilkit-agent-mcp\n",
		filepath.Join(id, "tls.crt"), filepath.Join(id, "tls.key"), filepath.Join(id, "ca.crt")))
}

type sidecarProcess struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser
	log   *os.File
}

func startSidecar(t *testing.T, b harnessBinaries, l harnessLayout, controlAddr, backend, launchKey, podUID, envelope string) *sidecarProcess {
	t.Helper()
	logf, err := os.Create(filepath.Join(l.root, "sidecar.log"))
	require.NoError(t, err)
	cmd := exec.Command(b.sidecar)
	cmd.Env = []string{"ANVILKIT_SIDECAR_CONFIG=" + l.sidecarCfg, "ANVILKIT_SIDECAR_CONTROL_ADDRESS=" + controlAddr, "ANVILKIT_SIDECAR_SOCKETS_DIR=" + l.sockets,
		"ANVILKIT_SIDECAR_BACKEND=" + backend, "ANVILKIT_SIDECAR_LAUNCH_KEY=" + launchKey, "ANVILKIT_SIDECAR_POD_UID=" + podUID, "ANVILKIT_SIDECAR_LAUNCH_ENVELOPE=" + envelope}
	cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: 10002, Gid: 10002, Groups: []uint32{0, 10001}}}
	stdin, err := cmd.StdinPipe()
	require.NoError(t, err)
	cmd.Stdout, cmd.Stderr = logf, logf
	require.NoError(t, cmd.Start())
	p := &sidecarProcess{cmd: cmd, stdin: stdin, log: logf}
	t.Cleanup(func() {
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
			raw, _ := os.ReadFile(filepath.Join(l.root, "sidecar.log"))
			t.Logf("sidecar log:\n%s", raw)
		}
	})
	require.Eventually(t, func() bool {
		st, err := os.Lstat(filepath.Join(l.sockets, "trusted.sock"))
		return err == nil && st.Mode()&os.ModeSocket != 0
	}, 20*time.Second, 100*time.Millisecond, "the sidecar did not open its sockets")
	return p
}

// summary mirrors the supervisor's termination message.
type harnessSummary struct {
	Outcome       string                              `json:"outcome"`
	Verdict       string                              `json:"verdict"`
	FailureCode   string                              `json:"failureCode"`
	StageID       string                              `json:"stageId"`
	ResultHandle  string                              `json:"resultHandle"`
	Evidence      string                              `json:"evidenceHandle"`
	Duplicate     *struct{ Existing, SameStage bool } `json:"duplicateSubmission"`
	CandidateExit *int                                `json:"candidateExit"`
	Probes        map[string]string                   `json:"probes"`
	Error         string                              `json:"error"`
}

// runSupervisor starts the supervisor as a Job container would: the whole
// process holds SETUID, SETGID and SETPCAP and nothing else (the
// supervisor's own supervisor-exec constrains the calling thread and
// execs, so every thread of the new process carries that set), as UID 0
// with no_new_privs.
func runSupervisor(t *testing.T, b harnessBinaries, l harnessLayout, launchID, attemptID, envelope string) (int, harnessSummary, string) {
	t.Helper()
	cmd := exec.Command(b.supervisor, "supervisor-exec", "--", b.supervisor)
	cmd.Env = []string{"ANVILKIT_CODEGEN_CONFIG=" + l.supervisorCfg, "ANVILKIT_LAUNCH_ID=" + launchID, "ANVILKIT_ATTEMPT_ID=" + attemptID, "ANVILKIT_LAUNCH_ENVELOPE=" + envelope, "PATH=/usr/bin:/bin"}
	out, err := cmd.CombinedOutput()
	code := 0
	if err != nil {
		var exit *exec.ExitError
		require.ErrorAs(t, err, &exit, string(out))
		code = exit.ExitCode()
	}
	var s harnessSummary
	raw, rerr := os.ReadFile(l.termination)
	if rerr == nil {
		require.NoError(t, json.Unmarshal(raw, &s), string(raw))
	}
	return code, s, string(out)
}

func harnessEnvelope(launchID, launchKey, opID, attemptID, executionEpoch, launchEpoch, revision string, deadline time.Time, inputDigest string) string {
	return fmt.Sprintf(`{"schemaVersion":1,"launchId":%q,"launchKey":%q,"operationId":%q,"attemptId":%q,"profileId":"harness-wiring-dev-v1","profileRevision":%q,"jobKind":"codegen","executionEpoch":%q,"launchEpoch":%q,"deadline":%q,"inputs":[{"name":"fixed-input","digest":%q}]}`,
		launchID, launchKey, opID, attemptID, revision, executionEpoch, launchEpoch, deadline.UTC().Format(time.RFC3339), inputDigest)
}

// trustedCall sends one request over trusted.sock as the root test process.
func trustedCall(t *testing.T, socket, method, path string, headers map[string]string, body []byte) (int, map[string]any) {
	t.Helper()
	tr := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}, DisableKeepAlives: true}
	c := &http.Client{Transport: tr, Timeout: 30 * time.Second}
	req, err := http.NewRequest(method, "http://sidecar"+path, strings.NewReader(string(body)))
	require.NoError(t, err)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := c.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out
}

// TestTrustedHarnessProcessFlow runs the P09 trusted flow with real
// processes on the host against the development foundation's Control and
// MinIO: the access sidecar as UID 10002, the supervisor as root with the
// reviewed capability set, the fixed candidate as UID 10001. The physical
// instance is registered by the test in the launcher's place (a fake Pod
// identity, as the P08 acceptance scenario does): what is real is the
// kernel boundary between the three identities, the sidecar's scope
// lookup, the scoped transfer to the object store and Control's
// acceptance. It qualifies no Kubernetes runtime.
func TestTrustedHarnessProcessFlow(t *testing.T) {
	if os.Geteuid() != 0 {
		skipMissing(t, "root", "the harness processes need a root caller (UID 10002 sidecar, UID 10001 candidate)")
	}
	if os.Getenv("ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT") == "" {
		skipMissing(t, "ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT", "not set; source .local/dev/env.sh after deploy/dev/up.sh")
	}
	e := setupProcesses(t, false, "", "", true)
	root := repoRoot(t)
	b := buildHarness(t, root)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
	defer cancel()
	ops, exec := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	profile, err := jobschema.ProfileByID("harness-wiring-dev-v1")
	require.NoError(t, err)
	fixture, err := os.ReadFile(filepath.Join(supervisorRepo(root), "fixtures", "fixed-input.txt"))
	require.NoError(t, err)
	inputDigest := digestOf(string(fixture))
	const backend = "host-process"

	created, err := ops.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-harness", ActorId: "user_a", RequestDigest: digestOf("harness")},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK,
		Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.NoError(t, err)
	opID := created.GetOperation().GetOperationId()
	opened, err := exec.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{
		Command:     &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-harness-open", ActorId: "user_a", RequestDigest: digestOf("open")},
		OperationId: opID, StepId: "codegen", VisitOrdinal: "0", ProfileId: "harness-wiring-dev-v1",
	})
	require.NoError(t, err)
	attempt := opened.GetAttempt()
	deadline := time.Now().Add(6 * time.Minute)
	launchKey := "hp-" + strings.ToLower(run[len(run)-8:])
	launch, err := exec.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
		Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run + "-harness", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
		AttemptId: attempt.GetAttemptId(), LaunchKey: launchKey, Backend: backend, ImageDigest: profile.Image.Digest, Deadline: timestamppb.New(deadline),
	})
	require.NoError(t, err)
	envelope := harnessEnvelope(launch.GetLaunchId(), launchKey, opID, attempt.GetAttemptId(), attempt.GetExecutionEpoch(), launch.GetLaunchEpoch(), profile.Revision, deadline, inputDigest)
	register := func(cmdID, pod string) *controlv1.PhysicalInstance {
		resp, err := exec.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: cmdID, ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf(cmdID)},
			AttemptId: attempt.GetAttemptId(), LaunchKey: launchKey, Backend: backend, JobUid: "job-" + run, PodUid: pod, ImageDigest: profile.Image.Digest,
		})
		require.NoError(t, err)
		return resp.GetInstance()
	}

	t.Run("the sidecar has no scope before the launcher registered the Pod", func(t *testing.T) {
		l := harnessDirs(t, root, b, "unregistered")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-unregistered", envelope)
		code, out := trustedCall(t, filepath.Join(l.sockets, "trusted.sock"), "GET", "/v1/scope", nil, nil)
		require.Equal(t, 503, code)
		require.Equal(t, "SCOPE_UNAVAILABLE", out["code"])
		code, _ = trustedCall(t, filepath.Join(l.sockets, "trusted.sock"), "POST", "/v1/transfers", map[string]string{"X-Anvilkit-Class": "result"}, []byte("x"))
		require.Equal(t, 503, code, "nothing is transferred without a registered scope")
	})

	owner := register("reg-"+run+"-owner", "pod-"+run+"-owner")
	require.True(t, owner.GetCurrent())

	var stageID string
	t.Run("the current instance completes the trusted flow and acceptance is idempotent", func(t *testing.T) {
		l := harnessDirs(t, root, b, "owner")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-owner", envelope)
		code, summary, log := runSupervisor(t, b, l, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		require.Equal(t, 0, code, log)
		require.Equal(t, "completed", summary.Outcome, log)
		require.Equal(t, "certified", summary.Verdict)
		require.NotEmpty(t, summary.StageID)
		require.NotNil(t, summary.Duplicate)
		require.True(t, summary.Duplicate.Existing && summary.Duplicate.SameStage, "the second submission reentered the accepted stage")
		stageID = summary.StageID
		stage, err := exec.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attempt.GetAttemptId(), TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, stageID, stage.GetStage().GetStageId())
		require.Equal(t, owner.GetInstanceId(), stage.GetStage().GetInstanceId())
		require.Equal(t, controlv1.Verdict_VERDICT_CERTIFIED, stage.GetStage().GetVerdict())
		require.Equal(t, sidecarObserver, stage.GetStage().GetObserverIdentity())
		require.Len(t, stage.GetStage().GetArtifacts(), 2, "the result and the evidence are bound as finalized objects")
		classes := map[string]string{}
		for _, a := range stage.GetStage().GetArtifacts() {
			require.NotEmpty(t, a.GetObjectVersion())
			classes[a.GetClass()] = a.GetHandle()
		}
		require.Equal(t, summary.ResultHandle, classes["result"])
		require.Equal(t, summary.Evidence, classes["evidence"])
		var stages int
		require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM stage_manifests WHERE operation_id = $1", opID).Scan(&stages))
		require.Equal(t, 1, stages)
		// The candidate observed its boundary from inside UID 10001.
		p := summary.Probes
		require.Equal(t, "true", p["capabilities-empty"], "%v", p)
		require.Equal(t, "1", p["no-new-privs"])
		require.Equal(t, "true", p["groups-empty"])
		require.Equal(t, "0", p["inherited-fds"])
		require.Equal(t, "EPERM", p["setuid-0"])
		require.Equal(t, "EACCES", p["read:verdict.json"])
		require.Equal(t, "EACCES", p["read:resources.json"])
		require.Equal(t, "EACCES", p["read:fixed-input.txt"])
		require.Equal(t, "EACCES", p["read:environ"])
		require.Equal(t, "EACCES", p["write:manifest.json"])
		require.Equal(t, "EACCES", p["connect:trusted.sock"])
		require.Equal(t, "200 ", p["candidate:inputs-permitted"])
		require.Equal(t, "403 ROUTE_FORBIDDEN", p["candidate:results"])
		require.Equal(t, "403 ROUTE_FORBIDDEN", p["candidate:scope"])
		require.Equal(t, "503 DEPENDENCY_UNAVAILABLE", p["candidate:model-relay"])
		require.Equal(t, "CLOSED", p["candidate:fd-delegation"], "a delegated descriptor over the candidate socket is refused by the real sidecar")
		t.Logf("host network probes (no syscall profile on the host): af-inet=%s af-inet6=%s", p["socket:af-inet"], p["socket:af-inet6"])
	})

	t.Run("a duplicate Pod is registered without authority and its harness runs nothing", func(t *testing.T) {
		dup := register("reg-"+run+"-dup", "pod-"+run+"-dup")
		require.False(t, dup.GetCurrent())
		l := harnessDirs(t, root, b, "dup")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-dup", envelope)
		code, summary, log := runSupervisor(t, b, l, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		require.Equal(t, 1, code, log)
		require.Equal(t, "infrastructure_failed", summary.Outcome)
		require.Contains(t, summary.Error, "NO_AUTHORITY")
		require.Nil(t, summary.CandidateExit, "the candidate never started")
		var stages int
		require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM stage_manifests WHERE operation_id = $1", opID).Scan(&stages))
		require.Equal(t, 1, stages, "no second acceptance")
	})

	t.Run("a stale scope is refused after the attempt closed", func(t *testing.T) {
		closed, err := exec.CloseAttempt(ctx, &controlv1.CloseAttemptRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: attempt.GetAttemptId() + ":close", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("close")},
			AttemptId: attempt.GetAttemptId(), Outcome: controlv1.AttemptOutcome_ATTEMPT_OUTCOME_COMPLETED, Cleanup: controlv1.CleanupState_CLEANUP_STATE_COMPLETE,
		})
		require.NoError(t, err)
		require.Equal(t, controlv1.AttemptState_ATTEMPT_STATE_CLOSED, closed.GetAttempt().GetState())
		// The owner Pod's registration is still the historically current
		// instance of the attempt (the row is never rewritten), but the
		// closed attempt carries no execution authority: the sidecar, which
		// reads Control before every protected request, answers no scope,
		// stages and serves no input, begins no transfer and submits no
		// result — nothing of it reaches Control on the earlier authority.
		var transfersBefore int
		require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM artifact_transfers WHERE attempt_id = $1", attempt.GetAttemptId()).Scan(&transfersBefore))
		l := harnessDirs(t, root, b, "stale")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-owner", envelope)
		scope, err := exec.GetInstance(ctx, &controlv1.GetInstanceRequest{Backend: backend, LaunchKey: launchKey, PodUid: "pod-" + run + "-owner"})
		require.NoError(t, err)
		require.True(t, scope.GetInstance().GetCurrent(), "the instance row keeps its historical current flag")
		require.Equal(t, controlv1.AttemptState_ATTEMPT_STATE_CLOSED, scope.GetAttempt().GetState())
		require.NotNil(t, scope.GetOperation(), "the operation travels with the registration")
		var code int
		var out map[string]any
		require.Eventually(t, func() bool {
			code, out = trustedCall(t, filepath.Join(l.sockets, "trusted.sock"), "GET", "/v1/scope", nil, nil)
			return code != 503
		}, 20*time.Second, 200*time.Millisecond, "the sidecar reached Control")
		require.Equal(t, 403, code, "%v", out)
		require.Equal(t, "STALE_EXECUTION", out["code"], "a closed attempt grants no scope: %v", out)
		code, out = trustedCall(t, filepath.Join(l.sockets, "trusted.sock"), "PUT", "/v1/inputs/fixed-input", nil, fixture)
		require.Equal(t, 403, code, "%v", out)
		require.Equal(t, "STALE_EXECUTION", out["code"], "nothing is staged for a closed attempt")
		code, out = trustedCall(t, filepath.Join(l.sockets, "trusted.sock"), "POST", "/v1/transfers", map[string]string{"X-Anvilkit-Class": "evidence", "Content-Type": "text/plain"}, []byte("late evidence\n"))
		require.Equal(t, 403, code, "%v", out)
		require.Equal(t, "STALE_EXECUTION", out["code"], "no transfer begins for a closed attempt: %v", out)
		code, out = trustedCall(t, filepath.Join(l.sockets, "trusted.sock"), "POST", "/v1/results", map[string]string{"Content-Type": "application/json"}, []byte(`{"verdict":"certified","observerIdentity":"late","manifest":{"schemaVersion":1,"launchId":"`+launch.GetLaunchId()+`","attemptId":"`+attempt.GetAttemptId()+`","jobKind":"codegen","profileId":"harness-wiring-dev-v1","verdict":"certified","outputs":[],"completedAt":"2026-09-16T00:00:00Z"}}`))
		require.Equal(t, 403, code, "%v", out)
		require.Equal(t, "STALE_EXECUTION", out["code"], "another result for the closed attempt never reaches acceptance")
		var transfersAfter, stages int
		require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM artifact_transfers WHERE attempt_id = $1", attempt.GetAttemptId()).Scan(&transfersAfter))
		require.Equal(t, transfersBefore, transfersAfter, "no transfer row was created for the closed attempt")
		require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM stage_manifests WHERE operation_id = $1", opID).Scan(&stages))
		require.Equal(t, 1, stages, "the original acceptance stands alone")
		_, err = exec.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attempt.GetAttemptId(), TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, codes.OK, status.Code(err))
	})
}
