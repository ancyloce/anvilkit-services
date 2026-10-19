//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	"github.com/ancyloce/anvilkit-agent-contracts/go/jobschema"
)

// validatorSummary mirrors the validator Job's termination message.
type validatorSummary struct {
	Outcome        string                              `json:"outcome"`
	Verdict        string                              `json:"verdict"`
	FailureCode    string                              `json:"failureCode"`
	StageID        string                              `json:"stageId"`
	Handles        map[string][]string                 `json:"handles"`
	Duplicate      *struct{ Existing, SameStage bool } `json:"duplicateSubmission"`
	StepIdentity   string                              `json:"stepIdentity"`
	Complete       *bool                               `json:"complete"`
	EvidenceDigest string                              `json:"evidenceDigest"`
	Detail         string                              `json:"detail"`
	Error          string                              `json:"error"`
}

func validatorEnvelope(profileRevision, launchID, launchKey, opID, attemptID, executionEpoch, launchEpoch string, deadline time.Time) string {
	return fmt.Sprintf(`{"schemaVersion":1,"launchId":%q,"launchKey":%q,"operationId":%q,"attemptId":%q,"profileId":"validator-fixture-v1","profileRevision":%q,"jobKind":"validator","executionEpoch":%q,"launchEpoch":%q,"deadline":%q,"inputs":[]}`,
		launchID, launchKey, opID, attemptID, profileRevision, executionEpoch, launchEpoch, deadline.UTC().Format(time.RFC3339))
}

// stageValidator copies the built validator package (dist, its locked
// install, profiles, fixtures) into a world-readable directory, as the image
// places it under /anvilkit/validator: the build and SSR steps run as UID
// 10001 and must traverse the trusted install, which a checkout under a
// private home directory does not allow.
func stageValidator(t *testing.T, root string, b harnessBinaries) string {
	t.Helper()
	src := filepath.Join(root, "jobs", "validator")
	dst := filepath.Join(b.dir, "validator")
	require.NoError(t, os.Mkdir(dst, 0o755))
	items := []string{"dist", "node_modules", "profiles", "package.json", "fixtures/host/ssr.mjs", "fixtures/host/browser/index.html", "fixtures/host/browser/host.tsx", "fixtures/host/browser/observer.js", "fixtures/component/hero"}
	// The host bundles the validator suite built (the trusted process rebuilds
	// them into the staged copy otherwise).
	if _, err := os.Stat(filepath.Join(src, "fixtures", "host", "browser", "dist", "manifest.json")); err == nil {
		items = append(items, "fixtures/host/browser/dist")
	}
	for _, item := range items {
		require.NoError(t, os.MkdirAll(filepath.Dir(filepath.Join(dst, item)), 0o755))
		out, err := exec.Command("cp", "-a", filepath.Join(src, item), filepath.Join(dst, item)).CombinedOutput()
		require.NoError(t, err, string(out))
	}
	// The browser of the mandatory browser-host check: the Chromium headless
	// shell of the pinned Playwright release, copied from the developer's
	// cache (a private home directory again) under the staged package, where
	// the step identity can execute it (PLAYWRIGHT_BROWSERS_PATH).
	require.NoError(t, os.Mkdir(filepath.Join(dst, "ms-playwright"), 0o755))
	shell := headlessShellDir(t, src)
	out, err := exec.Command("cp", "-a", shell, filepath.Join(dst, "ms-playwright", filepath.Base(shell))).CombinedOutput()
	require.NoError(t, err, string(out))
	// The Node executable the steps inherit (process.execPath) must be
	// reachable by UID 10001; a developer's node under a private home
	// directory is not, so the staged package carries its own copy.
	node, err := exec.LookPath("node")
	require.NoError(t, err)
	node, err = filepath.EvalSymlinks(node)
	require.NoError(t, err)
	require.NoError(t, os.Mkdir(filepath.Join(dst, "bin"), 0o755))
	out, err = exec.Command("cp", node, filepath.Join(dst, "bin", "node")).CombinedOutput()
	require.NoError(t, err, string(out))
	out, err = exec.Command("chmod", "-R", "a+rX", dst).CombinedOutput()
	require.NoError(t, err, string(out))
	return dst
}

// headlessShellDir is the directory of the Chromium headless shell the
// validator's pinned Playwright uses (its browsers.json names the build),
// in the developer's Playwright cache; the test is skipped without it.
func headlessShellDir(t *testing.T, pkg string) string {
	t.Helper()
	playwright, err := filepath.EvalSymlinks(filepath.Join(pkg, "node_modules", "playwright"))
	require.NoError(t, err)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(playwright), "playwright-core", "browsers.json"))
	require.NoError(t, err)
	var doc struct {
		Browsers []struct {
			Name, Revision string
		} `json:"browsers"`
	}
	require.NoError(t, json.Unmarshal(raw, &doc))
	for _, b := range doc.Browsers {
		if b.Name != "chromium-headless-shell" {
			continue
		}
		cache := os.Getenv("PLAYWRIGHT_BROWSERS_PATH")
		if cache == "" {
			home, err := os.UserHomeDir()
			require.NoError(t, err)
			cache = filepath.Join(home, ".cache", "ms-playwright")
		}
		dir := filepath.Join(cache, "chromium_headless_shell-"+b.Revision)
		if _, err := os.Stat(dir); err != nil {
			skipMissing(t, "tool:chromium-headless-shell", "Playwright's Chromium headless shell %s is not installed under %s (pnpm exec playwright install --only-shell chromium in jobs/validator)", b.Revision, cache)
		}
		return dir
	}
	t.Fatal("playwright-core/browsers.json names no chromium-headless-shell")
	return ""
}

// runValidator starts the validator Job's trusted process as a Job container
// would: node dist/job.js as UID 0 with the bounding set reduced to SETUID,
// SETGID and SETPCAP (setpriv, so that no CAP_CHOWN, CAP_DAC_OVERRIDE or
// CAP_KILL of the test's own root hides a defect the Pod would show), the
// build, SSR render and browser steps as UID 10001 and the SSR harness as
// UID 10003 through the process's own setpriv drop. It runs in its own PID
// namespace, as in its Pod: the Job stops every process of its step UIDs it
// can see and refuses to start beside one, and the host runs unrelated
// UID-10001 processes (P0.8).
// It returns the exit code, the termination message and the log.
func runValidator(t *testing.T, root, pkg string, l harnessLayout, cfgPath, launchID, attemptID, envelope string) (int, validatorSummary, string) {
	t.Helper()
	cmd := exec.Command("unshare", "--pid", "--fork", "--mount-proc", "--",
		"setpriv", "--bounding-set=-all,+setuid,+setgid,+setpcap", "--inh-caps=-all", "--", filepath.Join(pkg, "bin", "node"), filepath.Join(pkg, "dist", "job.js"))
	cmd.Dir = pkg
	cmd.Env = []string{
		"ANVILKIT_VALIDATOR_CONFIG=" + cfgPath, "ANVILKIT_VALIDATOR_CONTRACTS_DIR=" + filepath.Join(root, "contracts"),
		"ANVILKIT_LAUNCH_ID=" + launchID, "ANVILKIT_ATTEMPT_ID=" + attemptID, "ANVILKIT_LAUNCH_ENVELOPE=" + envelope,
		"PATH=" + os.Getenv("PATH"), "HOME=" + l.root, "PLAYWRIGHT_BROWSERS_PATH=" + filepath.Join(pkg, "ms-playwright"),
	}
	out, err := cmd.CombinedOutput()
	code := 0
	if err != nil {
		var exit *exec.ExitError
		require.ErrorAs(t, err, &exit, string(out))
		code = exit.ExitCode()
	}
	var s validatorSummary
	if raw, rerr := os.ReadFile(l.termination); rerr == nil {
		require.NoError(t, json.Unmarshal(raw, &s), string(raw))
	}
	return code, s, string(out)
}

func validatorDirs(t *testing.T, pkg string, b harnessBinaries, name string) (harnessLayout, string) {
	t.Helper()
	base := filepath.Join(b.dir, name)
	l := harnessLayout{root: base, workspace: filepath.Join(base, "workspace"), verdict: filepath.Join(base, "verdict"), run: filepath.Join(base, "run"), termination: filepath.Join(base, "termination-log"), sidecarCfg: filepath.Join(base, "sidecar.yaml")}
	l.sockets = filepath.Join(l.run, "sockets")
	require.NoError(t, os.MkdirAll(base, 0o755))
	for _, d := range []string{l.workspace, l.verdict, l.run} {
		require.NoError(t, os.Mkdir(d, 0o777))
		require.NoError(t, os.Chmod(d, 0o777))
	}
	require.NoError(t, os.WriteFile(l.sidecarCfg, sidecarConfig(t, base), 0o644))
	cfg := filepath.Join(base, "validator.yaml")
	require.NoError(t, os.WriteFile(cfg, []byte(fmt.Sprintf("paths:\n  workspace: %s\n  verdict: %s\n  sockets: %s\n  termination_log: %s\n  fixed_source: %s\ncandidate:\n  identity: setpriv\n  uid: 10001\n  gid: 10001\nsidecar:\n  wait_ms: 30000\nhost_checks:\n  ssr: true\n  browser: true\n",
		l.workspace, l.verdict, l.sockets, l.termination, filepath.Join(pkg, "fixtures", "component", "hero"))), 0o644))
	return l, cfg
}

// TestComponentCertificationFlow runs the P10 chain with real processes on
// the host against the development foundation's Control and MinIO: the
// access sidecar as UID 10002, the validator's trusted process as root with
// its build and SSR steps as UID 10001 through setpriv, on the reviewed
// fixed component. The physical instance is registered by the test in the
// launcher's place (as the P08/P09 scenarios do). What is real is the
// complete-source, build and certification chain, the scoped transfers of
// the npm tarball, the browser module, the stylesheet and the evidence to
// the object store, and Control's acceptance binding them by exact object
// version. It qualifies no Kubernetes runtime and no candidate isolation.
func TestComponentCertificationFlow(t *testing.T) {
	if os.Geteuid() != 0 {
		skipMissing(t, "root", "the validator steps need a root caller (UID 10002 sidecar, UID 10001 steps)")
	}
	if os.Getenv("ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT") == "" {
		skipMissing(t, "ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT", "not set; source .local/dev/env.sh after deploy/dev/up.sh")
	}
	root := repoRoot(t)
	for _, tool := range []string{"node", "pnpm", "setpriv"} {
		if _, err := exec.LookPath(tool); err != nil {
			skipMissing(t, "tool:"+tool, "%s is not on PATH", tool)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "jobs", "validator", "dist", "job.js")); err != nil {
		skipMissing(t, "build:jobs/validator/dist", "jobs/validator/dist/job.js is not built (pnpm run build in jobs/validator)")
	}
	e := setupProcesses(t, false, "", "", true)
	b := buildHarness(t, root)
	pkg := stageValidator(t, root, b)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Minute)
	defer cancel()
	ops, exec := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	artifacts := controlv1.NewArtifactServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	profile, err := jobschema.ProfileByID("validator-fixture-v1")
	require.NoError(t, err)
	require.Equal(t, "validator", profile.JobKind)
	require.False(t, profile.CandidateCode)
	const backend = "host-process"

	created, err := ops.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-component", ActorId: "user_a", RequestDigest: digestOf("component")},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK,
		Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.NoError(t, err)
	opID := created.GetOperation().GetOperationId()
	opened, err := exec.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{
		Command:     &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-component-open", ActorId: "user_a", RequestDigest: digestOf("open")},
		OperationId: opID, StepId: "validator", VisitOrdinal: "0", ProfileId: "validator-fixture-v1",
	})
	require.NoError(t, err)
	attempt := opened.GetAttempt()
	deadline := time.Now().Add(10 * time.Minute)
	launchKey := "vc-" + strings.ToLower(run[len(run)-8:])
	launch, err := exec.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
		Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run + "-component", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
		AttemptId: attempt.GetAttemptId(), LaunchKey: launchKey, Backend: backend, ImageDigest: profile.Image.Digest, Deadline: timestamppb.New(deadline),
	})
	require.NoError(t, err)
	envelope := validatorEnvelope(profile.Revision, launch.GetLaunchId(), launchKey, opID, attempt.GetAttemptId(), attempt.GetExecutionEpoch(), launch.GetLaunchEpoch(), deadline)
	register := func(cmdID, pod string) *controlv1.PhysicalInstance {
		resp, err := exec.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: cmdID, ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf(cmdID)},
			AttemptId: attempt.GetAttemptId(), LaunchKey: launchKey, Backend: backend, JobUid: "job-" + run, PodUid: pod, ImageDigest: profile.Image.Digest,
		})
		require.NoError(t, err)
		return resp.GetInstance()
	}
	owner := register("reg-"+run+"-owner", "pod-"+run+"-owner")
	require.True(t, owner.GetCurrent())
	stages := func() int {
		var n int
		require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM stage_manifests WHERE operation_id = $1", opID).Scan(&n))
		return n
	}

	t.Run("a forged certification never creates an accepted stage", func(t *testing.T) {
		l, _ := validatorDirs(t, pkg, b, "forged")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-owner", envelope)
		socket := filepath.Join(l.sockets, "trusted.sock")
		bogus := "sha256:6666666666666666666666666666666666666666666666666666666666666666"
		forge := func(outputs string) (int, map[string]any) {
			manifest := fmt.Sprintf(`{"schemaVersion":1,"launchId":%q,"attemptId":%q,"jobKind":"validator","profileId":"validator-fixture-v1","verdict":"certified","outputs":[%s],"completedAt":"2026-09-16T12:00:00Z"}`, launch.GetLaunchId(), attempt.GetAttemptId(), outputs)
			body, _ := json.Marshal(map[string]any{"verdict": "certified", "failureCode": "", "observerIdentity": "anvilkit-validator-supervisor", "manifest": json.RawMessage(manifest)})
			return trustedCall(t, socket, "POST", "/v1/results", map[string]string{"Content-Type": "application/json"}, body)
		}
		// Deliverables that name no finalized object: nothing is embedded for a validator profile.
		code, out := forge(fmt.Sprintf(`{"class":"npm","digest":%q,"sizeBytes":"2637"}`, bogus))
		require.NotEqual(t, 200, code, "%v", out)
		// A handle that is not a transfer of this scope.
		code, out = forge(fmt.Sprintf(`{"class":"npm","digest":%q,"sizeBytes":"2637","handle":"hdl_forged"}`, bogus))
		require.NotEqual(t, 200, code, "%v", out)
		// A real evidence transfer claimed as an npm deliverable (wrong class), and with a wrong digest.
		code, ev := trustedCall(t, socket, "POST", "/v1/transfers", map[string]string{"X-Anvilkit-Class": "evidence", "Content-Type": "application/json"}, []byte(`{"forged":true}`))
		require.Equal(t, 200, code, "%v", ev)
		handle, _ := ev["handle"].(string)
		digest, _ := ev["digest"].(string)
		code, out = forge(fmt.Sprintf(`{"class":"npm","digest":%q,"sizeBytes":"14","handle":%q}`, digest, handle))
		require.NotEqual(t, 200, code, "%v", out)
		code, out = forge(fmt.Sprintf(`{"class":"evidence","digest":%q,"sizeBytes":"14","handle":%q}`, bogus, handle))
		require.NotEqual(t, 200, code, "%v", out)
		require.Equal(t, 0, stages(), "no forged manifest was accepted")
	})

	var summary validatorSummary
	t.Run("the fixed component is built, certified, transferred and accepted; acceptance is idempotent", func(t *testing.T) {
		l, cfg := validatorDirs(t, pkg, b, "owner")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-owner", envelope)
		code, s, log := runValidator(t, root, pkg, l, cfg, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		require.Equal(t, 0, code, log)
		require.Equal(t, "completed", s.Outcome, log)
		require.Equal(t, "certified", s.Verdict, "%s %s\n%s", s.FailureCode, s.Detail, log)
		require.NotNil(t, s.Complete)
		require.True(t, *s.Complete, "every mandatory check of the validator profile passed in this run")
		require.Equal(t, "setpriv", s.StepIdentity)
		require.NotEmpty(t, s.StageID)
		require.NotNil(t, s.Duplicate)
		require.True(t, s.Duplicate.Existing && s.Duplicate.SameStage, "the second submission reentered the accepted stage")
		summary = s
		stage, err := exec.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attempt.GetAttemptId(), TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, s.StageID, stage.GetStage().GetStageId())
		require.Equal(t, owner.GetInstanceId(), stage.GetStage().GetInstanceId())
		require.Equal(t, controlv1.Verdict_VERDICT_CERTIFIED, stage.GetStage().GetVerdict())
		require.Equal(t, sidecarObserver, stage.GetStage().GetObserverIdentity())
		classes := map[string][]string{}
		for _, a := range stage.GetStage().GetArtifacts() {
			require.NotEmpty(t, a.GetObjectVersion(), "every bound artifact records its exact object version")
			classes[a.GetClass()] = append(classes[a.GetClass()], a.GetHandle())
			// The transfer behind every bound artifact is finalized with the same bytes.
			got, err := artifacts.GetTransfer(ctx, &controlv1.GetTransferRequest{Handle: a.GetHandle(), Scope: &controlv1.Scope{TenantId: "tenant_a", ActorId: "user_a"}})
			require.NoError(t, err)
			require.Equal(t, controlv1.TransferState_TRANSFER_STATE_FINALIZED, got.GetTransfer().GetState())
			require.Equal(t, a.GetDigest(), got.GetTransfer().GetExpectedDigest())
			require.Equal(t, a.GetObjectVersion(), got.GetTransfer().GetObjectVersion())
		}
		require.Len(t, classes["npm"], 1, "%v", classes)
		require.Len(t, classes["browser"], 1, "%v", classes)
		require.Len(t, classes["css"], 1, "%v", classes)
		require.Len(t, classes["evidence"], 1, "%v", classes)
		require.Equal(t, s.Handles["npm"], classes["npm"])
		require.Equal(t, s.Handles["browser"], classes["browser"])
		require.Equal(t, s.Handles["css"], classes["css"])
		require.Equal(t, s.Handles["evidence"], classes["evidence"])
		require.Equal(t, 1, stages())
		// The evidence the stage binds is the certification the trusted process wrote locally.
		raw, err := os.ReadFile(filepath.Join(l.verdict, "evidence.json"))
		require.NoError(t, err)
		var ev struct {
			Certification struct {
				Verdict        string `json:"verdict"`
				EvidenceDigest string `json:"evidenceDigest"`
				Bindings       struct {
					Npm     struct{ Digest string } `json:"npm"`
					Browser struct{ Digest string } `json:"browser"`
				} `json:"bindings"`
			} `json:"certification"`
		}
		require.NoError(t, json.Unmarshal(raw, &ev))
		require.Equal(t, "certified", ev.Certification.Verdict)
		require.Equal(t, s.EvidenceDigest, ev.Certification.EvidenceDigest)
		require.Equal(t, digestOf(string(raw)), stage.GetStage().GetArtifacts()[indexOfClass(stage.GetStage().GetArtifacts(), "evidence")].GetDigest())
		for _, a := range stage.GetStage().GetArtifacts() {
			switch a.GetClass() {
			case "npm":
				require.Equal(t, ev.Certification.Bindings.Npm.Digest, a.GetDigest(), "the bound npm object is the certified tarball")
			case "browser":
				require.Equal(t, ev.Certification.Bindings.Browser.Digest, a.GetDigest(), "the bound browser object is the certified module")
			}
		}
	})

	t.Run("a repeated launch of the same instance reenters the stage and creates nothing", func(t *testing.T) {
		l, cfg := validatorDirs(t, pkg, b, "repeat")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-owner", envelope)
		code, s, log := runValidator(t, root, pkg, l, cfg, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		// The attempt is RESULT_ACCEPTED: new transfers are refused (STALE_EXECUTION)
		// and the trusted flow reports that instead of a second stage.
		require.Equal(t, 1, code, log)
		require.Contains(t, s.Error, "STALE_EXECUTION", log)
		require.Equal(t, 1, stages())
		require.Equal(t, summary.StageID, mustStage(t, ctx, exec, attempt.GetAttemptId()))
	})

	t.Run("a duplicate Pod has no authority and certifies nothing", func(t *testing.T) {
		dup := register("reg-"+run+"-dup", "pod-"+run+"-dup")
		require.False(t, dup.GetCurrent())
		l, cfg := validatorDirs(t, pkg, b, "dup")
		startSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-dup", envelope)
		code, s, log := runValidator(t, root, pkg, l, cfg, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		require.Equal(t, 1, code, log)
		require.Equal(t, "infrastructure_failed", s.Outcome)
		require.Contains(t, s.Error, "NO_AUTHORITY")
		require.Empty(t, s.Verdict, "no chain ran without authority")
		require.Equal(t, 1, stages())
	})
}

func indexOfClass(list []*controlv1.ArtifactReference, class string) int {
	for i, a := range list {
		if a.GetClass() == class {
			return i
		}
	}
	return -1
}

func mustStage(t *testing.T, ctx context.Context, exec controlv1.ExecutionServiceClient, attemptID string) string {
	t.Helper()
	stage, err := exec.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attemptID, TenantId: "tenant_a"})
	require.NoError(t, err)
	return stage.GetStage().GetStageId()
}
