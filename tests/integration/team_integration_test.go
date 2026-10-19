//go:build integration

package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.yaml.in/yaml/v3"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	"github.com/ancyloce/anvilkit-agent-contracts/go/jobschema"
)

// teamSummary mirrors the supervisor's termination message in team mode.
type teamSummary struct {
	Outcome     string                              `json:"outcome"`
	Verdict     string                              `json:"verdict"`
	FailureCode string                              `json:"failureCode"`
	StageID     string                              `json:"stageId"`
	Duplicate   *struct{ Existing, SameStage bool } `json:"duplicateSubmission"`
	Team        *struct {
		Outcome      string `json:"outcome"`
		Rounds       int    `json:"rounds"`
		ReviewRounds int    `json:"reviewRounds"`
		Repairs      int    `json:"repairs"`
		Calls        int    `json:"calls"`
		Existing     bool   `json:"existingStage"`
		Error        string `json:"error"`
	} `json:"team"`
	Error string `json:"error"`
}

// stageTeam copies the built team package (dist, its locked install,
// package.json, the process protocol's contract copy) into a world-readable
// directory, as the image places it under /anvilkit/team: the Pi coder runs as UID 10001 from it, which a
// checkout under a private home directory does not allow. The coordinator
// runs as root from the same copy.
func stageTeam(t *testing.T, root string, b harnessBinaries) string {
	t.Helper()
	src := teamRepo(root)
	if _, err := os.Stat(filepath.Join(src, "dist", "coordinator.js")); err != nil {
		skipMissing(t, "build:jobs/codegen/team/dist", "jobs/codegen/team/dist is not built (pnpm run build in the team repository)")
	}
	dst := filepath.Join(b.dir, "team")
	require.NoError(t, os.Mkdir(dst, 0o755))
	for _, item := range []string{"dist", "node_modules", "package.json", "contract"} {
		out, err := exec.Command("cp", "-a", filepath.Join(src, item), filepath.Join(dst, item)).CombinedOutput()
		require.NoError(t, err, string(out))
	}
	out, err := exec.Command("chmod", "-R", "a+rX", dst).CombinedOutput()
	require.NoError(t, err, string(out))
	return dst
}

// teamProxyConfig is the P11 scenario's Proxy configuration with the team's
// reviewed tools (agent/team/tools.json) added to the controlled route: the
// Proxy serves a tool only when the route carries its exact schema.
func teamProxyConfig(t *testing.T, root, dir, listen, upstreamURL string) string {
	t.Helper()
	path := writeProxyConfig(t, root, dir, listen, upstreamURL)
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
	var b strings.Builder
	for _, tool := range doc.Tools {
		desc, _ := json.Marshal(tool.Description)
		// One line per schema: a JSON object is a YAML flow mapping only on one line inside a block sequence.
		var schema bytes.Buffer
		require.NoError(t, json.Compact(&schema, tool.InputSchema))
		fmt.Fprintf(&b, "      - name: %s\n        description: %s\n        input_schema: %s\n", tool.Name, desc, schema.String())
	}
	text, err := os.ReadFile(path)
	require.NoError(t, err)
	const anchor = "    tools:\n      - name: read_brief\n"
	require.Contains(t, string(text), anchor)
	updated := strings.Replace(string(text), anchor, "    tools:\n"+b.String()+"      - name: read_brief\n", 1)
	// Every call of the team states the attempt's absolute deadline; the
	// route's own bound must cover the team profile's deadline.
	require.Contains(t, updated, "      max_deadline: 10m\n")
	updated = strings.Replace(updated, "      max_deadline: 10m\n", "      max_deadline: 1h\n", 1)
	// The team declares and accounts every send at what Control reserves for
	// it: the route's full max_exposure, which the Proxy reserves whatever a
	// caller declares. A team configuration that states less would fund sends
	// Control denies.
	m := regexp.MustCompile(`max_exposure: \{ currency: USD, amount: "([0-9]+)" \}`).FindStringSubmatch(updated)
	require.NotNil(t, m, "the controlled route's max_exposure")
	routeBound, err := strconv.ParseInt(m[1], 10, 64)
	require.NoError(t, err)
	require.Equal(t, routeBound, readTeamBudget(t, root).perSend, "team.yaml route.exposurePerSend is the route's max_exposure")
	require.NoError(t, os.WriteFile(path, []byte(updated), 0o600))
	return path
}

// teamDirs is the harness layout of a team launch: the agent directory with
// the team's prompts and reviewed tools, the brief fixture, and the
// supervisor's configuration in team mode pointing at the staged team and
// validator packages.
func teamDirs(t *testing.T, root string, b harnessBinaries, teamPkg, validatorPkg, name string) harnessLayout {
	t.Helper()
	l := harnessDirs(t, root, b, name)
	agentTeam := filepath.Join(l.agent, "team")
	require.NoError(t, os.MkdirAll(filepath.Join(agentTeam, "prompts"), 0o700))
	for _, item := range []string{"tools.json", "prompts/planner.md", "prompts/retrieval.md", "prompts/coder.md", "prompts/code_reviewer.md", "prompts/security_reviewer.md"} {
		src, err := os.ReadFile(filepath.Join(teamRepo(root), "agent", "team", item))
		require.NoError(t, err)
		require.NoError(t, os.WriteFile(filepath.Join(agentTeam, item), src, 0o600))
	}
	brief, err := os.ReadFile(filepath.Join(teamRepo(root), "fixtures", "brief.json"))
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(filepath.Join(l.fixtures, "brief.json"), brief, 0o600))
	teamYaml, err := os.ReadFile(filepath.Join(teamRepo(root), "team.yaml"))
	require.NoError(t, err)
	text := string(teamYaml)
	for _, r := range [][2]string{
		{"package: /anvilkit/validator", "package: " + validatorPkg},
		{"node: /usr/local/bin/node", "node: " + filepath.Join(validatorPkg, "bin", "node")},
		{"contractsDir: /anvilkit/contracts", "contractsDir: " + filepath.Join(root, "contracts")},
		{"browsersPath: /anvilkit/ms-playwright", "browsersPath: " + filepath.Join(validatorPkg, "ms-playwright")},
	} {
		require.Contains(t, text, r[0])
		text = strings.Replace(text, r[0], r[1], 1)
	}
	teamCfg := filepath.Join(l.root, "team.yaml")
	require.NoError(t, os.WriteFile(teamCfg, []byte(text), 0o600))
	node := filepath.Join(validatorPkg, "bin", "node")
	require.NoError(t, os.WriteFile(l.supervisorCfg, []byte(fmt.Sprintf("paths:\n  workspace: %s\n  verdict: %s\n  sockets: %s\n  agent_dir: %s\n  fixtures: %s\n  termination_log: %s\ncandidate:\n  program: %s\n  timeout: 600s\nsidecar:\n  wait: 30s\nteam:\n  coordinator: [%s, %s]\n  coder: [%s, %s]\n  config: %s\n  contracts_dir: %s\n  timeout: 20m\n",
		l.workspace, l.verdict, l.sockets, l.agent, l.fixtures, l.termination, b.candidate, node, filepath.Join(teamPkg, "dist", "coordinator.js"), node, filepath.Join(teamPkg, "dist", "coder.js"), teamCfg, filepath.Join(root, "contracts"))), 0o600))
	return l
}

// runTeamSupervisor runs the team supervisor as PID 1 of its own PID
// namespace, as in its Pod (P0.8): the coordinator confirms the stop of the
// step identities below its launch root, and the validator it runs refuses
// to start beside a process of UID 10001 or 10003, which the host has.
func runTeamSupervisor(t *testing.T, b harnessBinaries, l harnessLayout, validatorPkg, launchID, attemptID, envelope string) (int, teamSummary, string) {
	t.Helper()
	cmd := exec.Command("unshare", "--pid", "--fork", "--mount-proc", "--", b.supervisor, "supervisor-exec", "--", b.supervisor, "team")
	cmd.Env = []string{"ANVILKIT_CODEGEN_CONFIG=" + l.supervisorCfg, "ANVILKIT_LAUNCH_ID=" + launchID, "ANVILKIT_ATTEMPT_ID=" + attemptID, "ANVILKIT_LAUNCH_ENVELOPE=" + envelope, "PATH=/usr/bin:/bin", "PLAYWRIGHT_BROWSERS_PATH=" + filepath.Join(validatorPkg, "ms-playwright")}
	out, err := cmd.CombinedOutput()
	code := 0
	if err != nil {
		var exit *exec.ExitError
		require.ErrorAs(t, err, &exit, string(out))
		code = exit.ExitCode()
	}
	var s teamSummary
	if raw, rerr := os.ReadFile(l.termination); rerr == nil {
		require.NoError(t, json.Unmarshal(raw, &s), string(raw))
	}
	return code, s, string(out)
}

func startTeamSidecar(t *testing.T, b harnessBinaries, l harnessLayout, controlAddr, backend, launchKey, podUID, envelope, proxyURL string) *sidecarProcess {
	t.Helper()
	logf, err := os.Create(filepath.Join(l.root, "sidecar.log"))
	require.NoError(t, err)
	cmd := exec.Command(b.sidecar)
	cmd.Env = []string{"ANVILKIT_SIDECAR_CONFIG=" + l.sidecarCfg, "ANVILKIT_SIDECAR_CONTROL_ADDRESS=" + controlAddr, "ANVILKIT_SIDECAR_SOCKETS_DIR=" + l.sockets,
		"ANVILKIT_SIDECAR_BACKEND=" + backend, "ANVILKIT_SIDECAR_LAUNCH_KEY=" + launchKey, "ANVILKIT_SIDECAR_POD_UID=" + podUID, "ANVILKIT_SIDECAR_LAUNCH_ENVELOPE=" + envelope,
		"ANVILKIT_SIDECAR_MODEL_PROXY_URL=" + proxyURL, "ANVILKIT_SIDECAR_MODEL_PROXY_TOKEN=" + proxyTokenSidecar}
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

// heroWrites scripts the countable upstream as the team's controlled model:
// the Planner's plan, the coder's writes of the reviewed fixed Hero
// component file by file and its closing sentence, then the two reviewers'
// findings. The order is the team's fixed order (plan, code, review); the
// two reviewer calls run concurrently and receive the same answer.
func heroWrites(t *testing.T, root string) []upstreamBehavior {
	return heroWritesWith(t, root, nil)
}

// heroWritesWith is heroWrites with one file's first write replaced (the
// coder's defect), followed by the repair round's answers: the corrected
// write, the closing sentence and the second review round.
func heroWritesWith(t *testing.T, root string, defect map[string]string) []upstreamBehavior {
	t.Helper()
	hero := filepath.Join(root, "jobs", "validator", "fixtures", "component", "hero")
	var files []string
	require.NoError(t, filepath.Walk(hero, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.Mode().IsRegular() {
			rel, _ := filepath.Rel(hero, p)
			files = append(files, rel)
		}
		return nil
	}))
	sort.Strings(files)
	plan := map[string]any{"componentId": "cmp_hero_fixed", "puckType": "Hero", "packageName": "@anvilkit/hero-fixed", "version": "1.0.0",
		"steps": []map[string]any{{"id": "all", "title": "The fixed Hero", "files": files, "detail": "Declaration, package, lockfile, README, entry, component, stylesheet and mark."}}}
	planArgs, _ := json.Marshal(plan)
	out := []upstreamBehavior{{kind: "stream", text: []string{"Planning."}, tool: []string{"tc_plan", "submit_plan", string(planArgs)}}}
	contents := map[string]string{}
	for i, f := range files {
		content, err := os.ReadFile(filepath.Join(hero, f))
		require.NoError(t, err)
		contents[f] = string(content)
		text := string(content)
		if broken, ok := defect[f]; ok {
			text = broken
		}
		args, _ := json.Marshal(map[string]string{"path": f, "content": text})
		out = append(out, upstreamBehavior{kind: "stream", text: []string{"Writing " + f + "."}, tool: []string{fmt.Sprintf("tc_w_%d", i+1), "write", string(args)}})
	}
	out = append(out, upstreamBehavior{kind: "stream", text: []string{"The complete source is written."}})
	findings, _ := json.Marshal(map[string]any{"verdict": "pass", "findings": []any{}})
	review := func(id string) upstreamBehavior {
		return upstreamBehavior{kind: "stream", text: []string{"Reviewed."}, tool: []string{id, "submit_findings", string(findings)}}
	}
	out = append(out, review("tc_r1"), review("tc_r2"))
	if len(defect) > 0 {
		// The repair round: the corrected files, the closing sentence, the second review round.
		n := 0
		for _, f := range files {
			if _, ok := defect[f]; !ok {
				continue
			}
			n++
			args, _ := json.Marshal(map[string]string{"path": f, "content": contents[f]})
			out = append(out, upstreamBehavior{kind: "stream", text: []string{"Repairing " + f + "."}, tool: []string{fmt.Sprintf("tc_fix_%d", n), "write", string(args)}})
		}
		out = append(out, upstreamBehavior{kind: "stream", text: []string{"The repair is written."}}, review("tc_r3"), review("tc_r4"))
	}
	return out
}

// TestCodegenTeamFlow runs the P12 team with real processes on the host
// against the development foundation: two real Control processes, one real
// Model Proxy with the countable controlled upstream as its only route, the
// access sidecar as UID 10002, the supervisor as root with the reviewed
// capability set running the trusted coordinator (root) and the Pi coder
// as UID 10001 through its trampoline, the independent validator's chain
// over the sealed source (its build, SSR and browser steps as UID 10001
// through setpriv), the joint stage sealed and accepted by Control; then
// the same sealed source through the validator Job's own flow and Control's
// acceptance of the certified deliverables. The physical instances are
// registered by the test in the launcher's place (fake Pod identities, as
// the P08–P11 scenarios do). It qualifies no Kubernetes runtime and no
// candidate isolation; the controlled upstream establishes transport and
// team mechanics, never model quality.
func TestCodegenTeamFlow(t *testing.T) {
	if os.Geteuid() != 0 {
		skipMissing(t, "root", "the harness processes need a root caller (UID 10002 sidecar, UID 10001 candidate)")
	}
	if os.Getenv("ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT") == "" {
		skipMissing(t, "ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT", "not set; source .local/dev/env.sh after deploy/dev/up.sh")
	}
	for _, tool := range []string{"node", "pnpm", "setpriv"} {
		if _, err := exec.LookPath(tool); err != nil {
			skipMissing(t, "tool:"+tool, "%s not found", tool)
		}
	}
	root := repoRoot(t)
	scratch := t.TempDir()
	port := freePort(t)
	proxyURL := "http://127.0.0.1:" + port
	e := setupProcessesEnv(t, true, "", modelProxyControlConfig(t, root, scratch), true,
		[]string{"ANVILKIT_CONTROL_MODEL_PROXY_ADDRESS=" + proxyURL, "ANVILKIT_CONTROL_MODEL_PROXY_TOKEN=" + proxyTokenControl})
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()
	up := newModelUpstream(t)
	dist := buildModelProxy(t, root)
	principals := filepath.Join(scratch, "model-proxy-principals.json")
	require.NoError(t, os.WriteFile(principals, []byte(fmt.Sprintf(`{%q:{"principalId":"anvilkit-agent-workflow","kind":"workflow"},%q:{"principalId":"anvilkit-job-access-sidecar","kind":"sidecar"},%q:{"principalId":"anvilkit-agent-control","kind":"control"}}`, proxyTokenWorkflow, proxyTokenSidecar, proxyTokenControl)), 0o600))
	require.NoError(t, os.MkdirAll(filepath.Join(scratch, "a"), 0o700))
	proxy := &proxyProcess{name: "proxy-team", url: proxyURL, healthURL: "http://127.0.0.1:" + freePort(t), port: port, storeDir: filepath.Join(scratch, "store"), cfg: teamProxyConfig(t, root, filepath.Join(scratch, "a"), "127.0.0.1:"+port, up.srv.URL), principals: principals, dist: dist, controlAddr: e.controlAddr, root: root, t: t}
	proxy.start()
	t.Cleanup(func() {
		proxy.stop()
		if t.Failed() {
			t.Logf("proxy log:\n%s", logTail(t, filepath.Join(scratch, "a", "proxy-team.log"), 60))
		}
	})
	ops, execSvc := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	db := e.db()
	// The operations of this scenario are funded with the reviewed team's
	// aggregate exposure (readTeamBudget); every pool covers what is
	// allocated under it.
	budget := readTeamBudget(t, root)
	for _, row := range []struct {
		level         string
		tenant, actor *string
		cap           int64
	}{{"platform", nil, nil, 4 * budget.aggregate}, {"tenant", strPtr("tenant_a"), nil, 4 * budget.aggregate}, {"actor", strPtr("tenant_a"), strPtr("user_a"), 4 * budget.aggregate}} {
		_, err := db.Exec(ctx, "INSERT INTO budget_pools (pool_id, level, tenant_id, actor_id, period_start, period_end, currency, cap_amount) VALUES ($1, $2, $3, $4, now() - interval '1 hour', now() + interval '1 hour', 'USD', $5)",
			"pool-"+run+"-"+row.level, row.level, row.tenant, row.actor, row.cap)
		require.NoError(t, err)
	}
	t.Cleanup(func() {
		_, _ = db.Exec(context.Background(), "UPDATE budget_pools SET period_end = period_start + interval '1 second' WHERE pool_id LIKE $1", "pool-"+run+"-%")
	})

	b := buildHarness(t, root)
	teamPkg := stageTeam(t, root, b)
	validatorPkg := stageValidator(t, root, b)
	profile, err := jobschema.ProfileByID("codegen-team-dev-v1")
	require.NoError(t, err)
	require.Equal(t, "codegen", profile.JobKind)
	require.True(t, profile.CandidateCode, "the team profile executes model-written code")
	brief, err := os.ReadFile(filepath.Join(teamRepo(root), "fixtures", "brief.json"))
	require.NoError(t, err)
	const backend = "host-process"

	// The operation and its funded attempt under the team profile.
	created, err := ops.CreateOperation(ctx, &controlv1.CreateOperationRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-team", ActorId: "user_a", RequestDigest: digestOf("team")},
		Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
		Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK,
		Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
	})
	require.NoError(t, err)
	opID := created.GetOperation().GetOperationId()
	opened, err := execSvc.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{
		Command:     &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-team-open", ActorId: "user_a", RequestDigest: digestOf("open")},
		OperationId: opID, StepId: "codegen", VisitOrdinal: "0", ProfileId: "codegen-team-dev-v1",
	})
	require.NoError(t, err)
	attempt := opened.GetAttempt()
	for _, level := range []string{"platform", "tenant", "actor"} {
		_, err := db.Exec(ctx, "INSERT INTO allocations (allocation_id, pool_id, operation_id, currency, amount) VALUES ($1, $2, $3, 'USD', $4)", "alloc-"+run+"-team-"+level, "pool-"+run+"-"+level, opID, budget.aggregate)
		require.NoError(t, err)
	}
	deadline := time.Now().Add(15 * time.Minute)
	launchKey := "tm-" + strings.ToLower(run[len(run)-8:])
	launch, err := execSvc.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
		Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run + "-team", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
		AttemptId: attempt.GetAttemptId(), LaunchKey: launchKey, Backend: backend, ImageDigest: profile.Image.Digest, Deadline: timestamppb.New(deadline),
	})
	require.NoError(t, err)
	envelope := fmt.Sprintf(`{"schemaVersion":1,"launchId":%q,"launchKey":%q,"operationId":%q,"attemptId":%q,"profileId":"codegen-team-dev-v1","profileRevision":%q,"jobKind":"codegen","executionEpoch":%q,"launchEpoch":%q,"deadline":%q,"inputs":[{"name":"brief","digest":%q}]}`,
		launch.GetLaunchId(), launchKey, opID, attempt.GetAttemptId(), profile.Revision, attempt.GetExecutionEpoch(), launch.GetLaunchEpoch(), deadline.UTC().Format(time.RFC3339), digestOf(string(brief)))
	register := func(cmdID, pod string) *controlv1.PhysicalInstance {
		resp, err := execSvc.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
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
		require.NoError(t, db.QueryRow(ctx, "SELECT count(*) FROM stage_manifests WHERE operation_id = $1", opID).Scan(&n))
		return n
	}
	dispatches := func() (n int, states []string) {
		rows, err := db.Query(ctx, "SELECT state FROM dispatches WHERE call_id LIKE $1 ORDER BY call_id", attempt.GetAttemptId()+":%")
		require.NoError(t, err)
		defer rows.Close()
		for rows.Next() {
			var s string
			require.NoError(t, rows.Scan(&s))
			states = append(states, s)
			n++
		}
		return n, states
	}

	var summary teamSummary
	var ownerLayout harnessLayout
	var teamSource *controlv1.ArtifactReference
	t.Run("the team produces the complete source through controlled calls, validates it independently and Control accepts the joint stage", func(t *testing.T) {
		script := heroWrites(t, root)
		up.next(script...)
		before := up.receives.Load()
		l := teamDirs(t, root, b, teamPkg, validatorPkg, "owner")
		ownerLayout = l
		startTeamSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-owner", envelope, proxyURL)
		code, s, log := runTeamSupervisor(t, b, l, validatorPkg, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		if code != 0 || s.Verdict != "certified" {
			coord, _ := os.ReadFile(filepath.Join(l.verdict, "team", "coordinator.log"))
			t.Logf("coordinator log:\n%s", coord)
		}
		require.Equal(t, 0, code, log)
		require.Equal(t, "completed", s.Outcome, log)
		require.Equal(t, "certified", s.Verdict, "%s %s", s.FailureCode, s.Error)
		require.NotNil(t, s.Team)
		require.Equal(t, "certified", s.Team.Outcome)
		require.Equal(t, 1, s.Team.Rounds)
		require.Equal(t, 1, s.Team.ReviewRounds)
		require.Equal(t, 0, s.Team.Repairs)
		require.Equal(t, len(script), s.Team.Calls, "one controlled call per scripted answer: plan, the writes, the closing sentence, two reviews")
		require.Equal(t, int32(len(script)), up.receives.Load()-before, "every call is exactly one physical receive at the upstream")
		summary = s
		stage, err := execSvc.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attempt.GetAttemptId(), TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, s.StageID, stage.GetStage().GetStageId())
		require.Equal(t, owner.GetInstanceId(), stage.GetStage().GetInstanceId())
		require.Equal(t, controlv1.Verdict_VERDICT_CERTIFIED, stage.GetStage().GetVerdict())
		require.Equal(t, sidecarObserver, stage.GetStage().GetObserverIdentity())
		classes := map[string]*controlv1.ArtifactReference{}
		for _, a := range stage.GetStage().GetArtifacts() {
			require.NotEmpty(t, a.GetObjectVersion())
			classes[a.GetClass()] = a
		}
		require.Len(t, classes, 3)
		for _, c := range []string{"source", "stage", "evidence"} {
			require.Contains(t, classes, c, "the joint stage binds the sealed source, the stage archive and the evidence")
		}
		teamSource = classes["source"]
		require.Equal(t, 1, stages())
		// Control admitted every call once; the Proxy's records settled them.
		n, states := dispatches()
		require.Equal(t, len(script), n, "one admitted dispatch per controlled call")
		for _, st := range states {
			require.Equal(t, "observed", st, "the Proxy's observation of every call reached Control")
		}
		// The sealed source of the round is the reviewed fixed Hero, byte for byte.
		manifestRaw, err := os.ReadFile(filepath.Join(l.verdict, "team", "stage", "manifest.json"))
		require.NoError(t, err)
		var manifest struct {
			Source struct {
				ManifestDigest string `json:"manifestDigest"`
				Files          int    `json:"files"`
			} `json:"source"`
			Validation struct {
				Status string `json:"status"`
			} `json:"validation"`
			Counters struct{ Rounds, ReviewRounds, Repairs int } `json:"counters"`
		}
		require.NoError(t, json.Unmarshal(manifestRaw, &manifest))
		require.Equal(t, 8, manifest.Source.Files)
		require.Equal(t, "certified", manifest.Validation.Status)
	})

	t.Run("a duplicate Pod is registered without authority and its team runs nothing", func(t *testing.T) {
		dup := register("reg-"+run+"-dup", "pod-"+run+"-dup")
		require.False(t, dup.GetCurrent())
		before := up.receives.Load()
		l := teamDirs(t, root, b, teamPkg, validatorPkg, "dup")
		startTeamSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-dup", envelope, proxyURL)
		code, s, log := runTeamSupervisor(t, b, l, validatorPkg, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		require.Equal(t, 1, code, log)
		require.Equal(t, "infrastructure_failed", s.Outcome)
		require.Contains(t, s.Error, "NO_AUTHORITY")
		require.Nil(t, s.Team, "the coordinator never started")
		require.Equal(t, before, up.receives.Load())
		require.Equal(t, 1, stages())
	})

	t.Run("restart after acceptance: the attempt admits no new scope, nothing runs or is sent; the accepted stage is readable and the local objects prove against it, an altered one is refused", func(t *testing.T) {
		before := up.receives.Load()
		l := ownerLayout
		// A restarted trusted process finds the verdict tree with the sealed
		// stage; the candidate's own tree is stale and, owned by UID 10001,
		// not the supervisor's to remove (no CAP_DAC_OVERRIDE): the test
		// clears it as a fresh Pod would come without one.
		for _, stale := range []string{"w", "round", "validation"} {
			require.NoError(t, os.RemoveAll(filepath.Join(l.workspace, stale)))
		}
		startTeamSidecar(t, b, l, e.controlAddr, backend, launchKey, "pod-"+run+"-owner", envelope, proxyURL)
		code, s, log := runTeamSupervisor(t, b, l, validatorPkg, launch.GetLaunchId(), attempt.GetAttemptId(), envelope)
		require.Equal(t, 1, code, log)
		require.Equal(t, "infrastructure_failed", s.Outcome)
		require.Contains(t, s.Error, "STALE_EXECUTION", "an accepted attempt grants no new execution scope")
		require.Nil(t, s.Team, "the coordinator never started")
		require.Equal(t, before, up.receives.Load(), "no physical send")
		require.Equal(t, 1, stages())
		// Control's accepted stage is readable under the result authority (the recovery's first question) and binds
		// exactly the objects the local result manifest names.
		socket := filepath.Join(l.sockets, "trusted.sock")
		status, accepted := trustedCall(t, socket, "GET", "/v1/results", nil, nil)
		require.Equal(t, 200, status, "%v", accepted)
		require.Equal(t, summary.StageID, accepted["stageId"])
		require.Len(t, accepted["artifacts"], 3)
		stageDir := filepath.Join(l.verdict, "team", "stage")
		localRaw, err := os.ReadFile(filepath.Join(stageDir, "result-manifest.json"))
		require.NoError(t, err)
		require.Equal(t, digestOf(string(localRaw)), accepted["resultDigest"], "the local result manifest is the accepted one")
		// The coordinator's own proof (StageStore.prove) against Control's record: intact objects prove; an altered
		// session (a mixed pair beside the same checkpoint) is refused as STALE_STAGE.
		// Written beside the staged package so its relative imports resolve there.
		prove := filepath.Join(teamPkg, "prove.mjs")
		require.NoError(t, os.WriteFile(prove, []byte(`
import { readFileSync } from "node:fs";
import path from "node:path";
import { StageStore } from "./dist/stage/store.js";
import { SidecarClient } from "./dist/adapters/sidecar.js";
const [dir, socket] = process.argv.slice(2);
const manifest = JSON.parse(readFileSync(path.join(dir, "manifest.json"), "utf8"));
const sidecar = new SidecarClient(socket);
const accepted = await sidecar.acceptedStage();
const store = new StageStore({ dir, sidecar, identity: manifest.identity, teamProfileDigest: manifest.teamProfileDigest, observerIdentity: "anvilkit-codegen-team", sourceLimits: { maxFiles: 256, maxFileBytes: 1048576, maxTotalBytes: 16777216 } });
const scope = { ...manifest.identity, current: true };
try {
	const proven = await store.prove(accepted, scope);
	console.log("PROVEN " + proven.accepted.stageId);
} catch (err) {
	console.log("REFUSED " + (err.code ?? "") + " " + err.message);
}
`), 0o644))
		runProve := func() string {
			cmd := exec.Command("node", prove, stageDir, socket)
			cmd.Dir = teamPkg
			out, err := cmd.CombinedOutput()
			require.NoError(t, err, string(out))
			return strings.TrimSpace(string(out))
		}
		require.Equal(t, "PROVEN "+summary.StageID, runProve())
		require.NoError(t, os.Chmod(filepath.Join(stageDir, "session.jsonl"), 0o600))
		require.NoError(t, os.WriteFile(filepath.Join(stageDir, "session.jsonl"), []byte("{}\n"), 0o600))
		require.Contains(t, runProve(), "REFUSED STALE_STAGE")
		require.Equal(t, before, up.receives.Load(), "still no physical send")
		require.Equal(t, 1, stages())
	})

	t.Run("a repairable defect of the independent validator sends the team back to the coder once; the repaired source is certified", func(t *testing.T) {
		// A dependency outside the build-support profile: the validator's
		// source contract refuses it (CANDIDATE_BUILD_FAILED, repairable by
		// the profile's map) before any build; the repair round rewrites it.
		broken := map[string]string{"package.json": `{"name":"@anvilkit/hero-fixed","version":"1.0.0","description":"Reviewed fixed Hero component of the P10 certification chain (DEVELOPMENT fixture).","license":"MIT","dependencies":{"@puckeditor/core":"0.23.0","react":"19.3.0","left-pad":"1.3.0"}}` + "\n"}
		script := heroWritesWith(t, root, broken)
		up.next(script...)
		before := up.receives.Load()
		rcreated, err := ops.CreateOperation(ctx, &controlv1.CreateOperationRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-team-repair", ActorId: "user_a", RequestDigest: digestOf("repair")},
			Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
			Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK,
			Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
		})
		require.NoError(t, err)
		rop := rcreated.GetOperation().GetOperationId()
		ropened, err := execSvc.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{
			Command:     &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-team-repair-open", ActorId: "user_a", RequestDigest: digestOf("open")},
			OperationId: rop, StepId: "codegen", VisitOrdinal: "0", ProfileId: "codegen-team-dev-v1",
		})
		require.NoError(t, err)
		rattempt := ropened.GetAttempt()
		for _, level := range []string{"platform", "tenant", "actor"} {
			_, err := db.Exec(ctx, "INSERT INTO allocations (allocation_id, pool_id, operation_id, currency, amount) VALUES ($1, $2, $3, 'USD', $4)", "alloc-"+run+"-team-repair-"+level, "pool-"+run+"-"+level, rop, budget.aggregate)
			require.NoError(t, err)
		}
		rkey := "tr-" + strings.ToLower(run[len(run)-8:])
		rlaunch, err := execSvc.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run + "-team-repair", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
			AttemptId: rattempt.GetAttemptId(), LaunchKey: rkey, Backend: backend, ImageDigest: profile.Image.Digest, Deadline: timestamppb.New(deadline),
		})
		require.NoError(t, err)
		renvelope := fmt.Sprintf(`{"schemaVersion":1,"launchId":%q,"launchKey":%q,"operationId":%q,"attemptId":%q,"profileId":"codegen-team-dev-v1","profileRevision":%q,"jobKind":"codegen","executionEpoch":%q,"launchEpoch":%q,"deadline":%q,"inputs":[{"name":"brief","digest":%q}]}`,
			rlaunch.GetLaunchId(), rkey, rop, rattempt.GetAttemptId(), profile.Revision, rattempt.GetExecutionEpoch(), rlaunch.GetLaunchEpoch(), deadline.UTC().Format(time.RFC3339), digestOf(string(brief)))
		_, err = execSvc.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "reg-" + run + "-repair", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("reg-repair")},
			AttemptId: rattempt.GetAttemptId(), LaunchKey: rkey, Backend: backend, JobUid: "job-" + run + "-r", PodUid: "pod-" + run + "-repair", ImageDigest: profile.Image.Digest,
		})
		require.NoError(t, err)
		l := teamDirs(t, root, b, teamPkg, validatorPkg, "repair")
		startTeamSidecar(t, b, l, e.controlAddr, backend, rkey, "pod-"+run+"-repair", renvelope, proxyURL)
		code, s, log := runTeamSupervisor(t, b, l, validatorPkg, rlaunch.GetLaunchId(), rattempt.GetAttemptId(), renvelope)
		if code != 0 || s.Verdict != "certified" {
			coord, _ := os.ReadFile(filepath.Join(l.verdict, "team", "coordinator.log"))
			t.Logf("coordinator log:\n%s", coord)
			for _, n := range []string{"1", "2"} {
				t.Logf("coder round %s log:\n%s", n, logTail(t, filepath.Join(l.verdict, "team", "coder-round-"+n+".log"), 30))
			}
		}
		require.Equal(t, 0, code, log)
		require.Equal(t, "certified", s.Verdict, "%s %s", s.FailureCode, s.Error)
		require.NotNil(t, s.Team)
		require.Equal(t, 2, s.Team.Rounds, "the coding round and one repair round")
		require.Equal(t, 1, s.Team.Repairs)
		require.Equal(t, 2, s.Team.ReviewRounds)
		require.Equal(t, len(script), s.Team.Calls)
		require.Equal(t, int32(len(script)), up.receives.Load()-before, "one physical receive per controlled call, the repair round included")
		manifestRaw, err := os.ReadFile(filepath.Join(l.verdict, "team", "stage", "manifest.json"))
		require.NoError(t, err)
		var manifest struct {
			Source struct {
				Round    int
				Revision string
			} `json:"source"`
			Validation struct {
				Round  int
				Status string
			} `json:"validation"`
			Counters struct{ Rounds, ReviewRounds, Repairs int } `json:"counters"`
		}
		require.NoError(t, json.Unmarshal(manifestRaw, &manifest))
		require.Equal(t, 2, manifest.Source.Round, "the stage binds the repaired round's source")
		require.Equal(t, "2", manifest.Source.Revision)
		require.Equal(t, "certified", manifest.Validation.Status)
		// The first validation refused the source; the evidence records both.
		evidenceRaw, err := os.ReadFile(filepath.Join(l.verdict, "team", "stage", "evidence.json"))
		require.NoError(t, err)
		var evidence struct {
			Validations []struct {
				Round       int    `json:"round"`
				Status      string `json:"status"`
				FailureCode string `json:"failureCode"`
			} `json:"validations"`
		}
		require.NoError(t, json.Unmarshal(evidenceRaw, &evidence))
		require.Len(t, evidence.Validations, 2)
		require.Equal(t, "repairable", evidence.Validations[0].Status)
		require.Equal(t, "CANDIDATE_BUILD_FAILED", evidence.Validations[0].FailureCode)
		require.Equal(t, "certified", evidence.Validations[1].Status)
	})

	t.Run("the sealed source goes through the validator Job's own flow and Control accepts the certified deliverables", func(t *testing.T) {
		// P0.8: the accepted stage's sealed source, bound to a second attempt of
		// the same operation as the Generation lifecycle binds it (the source
		// input names the stage artifact's handle, the sidecar loads it from
		// Control) under the source-binding development profile, with the
		// brief's allocated identity at the generation's revision.
		require.NotNil(t, teamSource, "the first scenario accepted the team stage")
		vprofile, err := jobschema.ProfileByID("validator-source-dev-v1")
		require.NoError(t, err)
		require.True(t, vprofile.BindsSource)
		var allocated struct {
			ComponentID string `json:"componentId"`
			PuckType    string `json:"puckType"`
			PackageName string `json:"packageName"`
		}
		require.NoError(t, json.Unmarshal(brief, &allocated))
		const certifiedRevision = "3"
		vopened, err := execSvc.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{
			Command:     &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-team-validate-open", ActorId: "user_a", RequestDigest: digestOf("open")},
			OperationId: opID, StepId: "validator", VisitOrdinal: "0", ProfileId: vprofile.ProfileID,
		})
		require.NoError(t, err)
		vattempt := vopened.GetAttempt()
		vdeadline := time.Now().Add(10 * time.Minute)
		vkey := "tv-" + strings.ToLower(run[len(run)-8:])
		vlaunch, err := execSvc.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run + "-team-validate", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
			AttemptId: vattempt.GetAttemptId(), LaunchKey: vkey, Backend: backend, ImageDigest: vprofile.Image.Digest, Deadline: timestamppb.New(vdeadline),
		})
		require.NoError(t, err)
		rawEnvelope, err := json.Marshal(map[string]any{
			"schemaVersion": 1, "launchId": vlaunch.GetLaunchId(), "launchKey": vkey, "operationId": opID, "attemptId": vattempt.GetAttemptId(),
			"profileId": vprofile.ProfileID, "profileRevision": vprofile.Revision, "jobKind": "validator",
			"executionEpoch": vattempt.GetExecutionEpoch(), "launchEpoch": vlaunch.GetLaunchEpoch(), "deadline": vdeadline.UTC().Format(time.RFC3339),
			"inputs":    []map[string]string{{"name": "source", "digest": teamSource.GetDigest(), "handle": teamSource.GetHandle()}},
			"component": map[string]string{"componentId": allocated.ComponentID, "puckType": allocated.PuckType, "packageName": allocated.PackageName, "sourceRevision": certifiedRevision},
		})
		require.NoError(t, err)
		venvelope := string(rawEnvelope)
		_, err = execSvc.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "reg-" + run + "-validate", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("reg-validate")},
			AttemptId: vattempt.GetAttemptId(), LaunchKey: vkey, Backend: backend, JobUid: "job-" + run + "-v", PodUid: "pod-" + run + "-validate", ImageDigest: vprofile.Image.Digest,
		})
		require.NoError(t, err)
		l, cfg := validatorDirs(t, validatorPkg, b, "team-validate")
		startSidecar(t, b, l, e.controlAddr, backend, vkey, "pod-"+run+"-validate", venvelope)
		code, s, log := runValidator(t, root, validatorPkg, l, cfg, vlaunch.GetLaunchId(), vattempt.GetAttemptId(), venvelope)
		require.Equal(t, 0, code, log)
		require.Equal(t, "completed", s.Outcome, log)
		require.Equal(t, "certified", s.Verdict, "%s %s\n%s", s.FailureCode, s.Detail, log)
		require.NotNil(t, s.Complete)
		require.True(t, *s.Complete)
		stage, err := execSvc.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: vattempt.GetAttemptId(), TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, controlv1.Verdict_VERDICT_CERTIFIED, stage.GetStage().GetVerdict())
		var classes []string
		for _, a := range stage.GetStage().GetArtifacts() {
			classes = append(classes, a.GetClass())
		}
		sort.Strings(classes)
		require.Equal(t, []string{"browser", "css", "evidence", "npm"}, classes)
		// The certification binds the launch's identity and revision (AC4),
		// never the image's configured revision.
		evidenceRaw, err := os.ReadFile(filepath.Join(l.verdict, "evidence.json"))
		require.NoError(t, err, "the Job's verdict directory holds the uploaded evidence")
		var evidence struct {
			SourceRevision string `json:"sourceRevision"`
			Certification  struct {
				Bindings struct {
					ComponentID    string `json:"componentId"`
					PuckType       string `json:"puckType"`
					PackageName    string `json:"packageName"`
					SourceRevision string `json:"sourceRevision"`
				} `json:"bindings"`
			} `json:"certification"`
		}
		require.NoError(t, json.Unmarshal(evidenceRaw, &evidence))
		require.Equal(t, certifiedRevision, evidence.SourceRevision)
		b := evidence.Certification.Bindings
		require.Equal(t, certifiedRevision, b.SourceRevision)
		require.Equal(t, allocated.PuckType, b.PuckType)
		require.Equal(t, allocated.PackageName, b.PackageName)
		require.Equal(t, allocated.ComponentID, b.ComponentID)
	})

	t.Run("an operation funded for one send: Control denies the coder's first send and the run ends model_denied, never as a candidate defect", func(t *testing.T) {
		// The reproduced historical failure, as a property: with the planner's
		// send admitted and settled, the coder's first send needs another full
		// route reservation the allocation cannot hold. Control denies it
		// (BUDGET_EXHAUSTED); the round ends there and the run is recorded as
		// model_denied — no reviewer, no validation, no repair, no candidate
		// verdict about the source the round did not finish.
		up.next(heroWrites(t, root)[0]) // the plan; nothing of the coder reaches the upstream
		before := up.receives.Load()
		dcreated, err := ops.CreateOperation(ctx, &controlv1.CreateOperationRequest{
			Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-team-denied", ActorId: "user_a", RequestDigest: digestOf("denied")},
			Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
			Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK,
			Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
		})
		require.NoError(t, err)
		dop := dcreated.GetOperation().GetOperationId()
		dopened, err := execSvc.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{
			Command:     &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-team-denied-open", ActorId: "user_a", RequestDigest: digestOf("open")},
			OperationId: dop, StepId: "codegen", VisitOrdinal: "0", ProfileId: "codegen-team-dev-v1",
		})
		require.NoError(t, err)
		dattempt := dopened.GetAttempt()
		for _, level := range []string{"platform", "tenant", "actor"} {
			_, err := db.Exec(ctx, "INSERT INTO allocations (allocation_id, pool_id, operation_id, currency, amount) VALUES ($1, $2, $3, 'USD', $4)", "alloc-"+run+"-team-denied-"+level, "pool-"+run+"-"+level, dop, budget.perSend)
			require.NoError(t, err)
		}
		dkey := "td-" + strings.ToLower(run[len(run)-8:])
		dlaunch, err := execSvc.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run + "-team-denied", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
			AttemptId: dattempt.GetAttemptId(), LaunchKey: dkey, Backend: backend, ImageDigest: profile.Image.Digest, Deadline: timestamppb.New(deadline),
		})
		require.NoError(t, err)
		denvelope := fmt.Sprintf(`{"schemaVersion":1,"launchId":%q,"launchKey":%q,"operationId":%q,"attemptId":%q,"profileId":"codegen-team-dev-v1","profileRevision":%q,"jobKind":"codegen","executionEpoch":%q,"launchEpoch":%q,"deadline":%q,"inputs":[{"name":"brief","digest":%q}]}`,
			dlaunch.GetLaunchId(), dkey, dop, dattempt.GetAttemptId(), profile.Revision, dattempt.GetExecutionEpoch(), dlaunch.GetLaunchEpoch(), deadline.UTC().Format(time.RFC3339), digestOf(string(brief)))
		_, err = execSvc.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "reg-" + run + "-denied", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("reg-denied")},
			AttemptId: dattempt.GetAttemptId(), LaunchKey: dkey, Backend: backend, JobUid: "job-" + run + "-d", PodUid: "pod-" + run + "-denied", ImageDigest: profile.Image.Digest,
		})
		require.NoError(t, err)
		l := teamDirs(t, root, b, teamPkg, validatorPkg, "denied")
		startTeamSidecar(t, b, l, e.controlAddr, backend, dkey, "pod-"+run+"-denied", denvelope, proxyURL)
		code, s, log := runTeamSupervisor(t, b, l, validatorPkg, dlaunch.GetLaunchId(), dattempt.GetAttemptId(), denvelope)
		if code != 0 || s.Team == nil || s.Team.Outcome != "model_denied" {
			coord, _ := os.ReadFile(filepath.Join(l.verdict, "team", "coordinator.log"))
			t.Logf("coordinator log:\n%s", coord)
		}
		require.Equal(t, 0, code, log)
		require.Equal(t, "completed", s.Outcome, "the trusted flow completed: the stage records the denial")
		require.Equal(t, "infrastructure_failed", s.Verdict, "a Control denial is never a verdict about the candidate")
		require.Equal(t, "OBSERVER_FAILED", s.FailureCode)
		require.NotNil(t, s.Team)
		require.Equal(t, "model_denied", s.Team.Outcome)
		require.Equal(t, 1, s.Team.Rounds)
		require.Equal(t, 0, s.Team.ReviewRounds)
		require.Equal(t, 0, s.Team.Repairs)
		require.Equal(t, int32(1), up.receives.Load()-before, "only the planner's send reached the upstream")
		stage, err := execSvc.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: dattempt.GetAttemptId(), TenantId: "tenant_a"})
		require.NoError(t, err)
		require.Equal(t, controlv1.Verdict_VERDICT_INFRASTRUCTURE_FAILED, stage.GetStage().GetVerdict())
		manifestRaw, err := os.ReadFile(filepath.Join(l.verdict, "team", "stage", "manifest.json"))
		require.NoError(t, err)
		var manifest struct {
			Outcome struct {
				Kind        string `json:"kind"`
				FailureCode string `json:"failureCode"`
			} `json:"outcome"`
			Validation *struct{} `json:"validation"`
		}
		require.NoError(t, json.Unmarshal(manifestRaw, &manifest))
		require.Equal(t, "model_denied", manifest.Outcome.Kind)
		require.Equal(t, "BUDGET_EXHAUSTED", manifest.Outcome.FailureCode)
		require.Nil(t, manifest.Validation, "nothing was validated")
	})
}

// teamBudget is the reviewed team configuration's accounting: what Control
// reserves for one send on the route (the Model Proxy reserves the route's
// full max_exposure for every send, whatever a caller declares) and the
// aggregate exposure the team may account. Every call costs at most what
// Control reserved for it and the team accounts each at that reservation,
// so an operation allocation that covers the aggregate is never exceeded by
// the team's own sends; a smaller one is Control's to deny, and the run then
// ends model_denied.
type teamBudget struct{ perSend, aggregate int64 }

func readTeamBudget(t *testing.T, root string) teamBudget {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(teamRepo(root), "team.yaml"))
	require.NoError(t, err)
	var team struct {
		Route struct {
			ExposurePerSend string `yaml:"exposurePerSend"`
		} `yaml:"route"`
		Aggregate struct {
			Exposure string `yaml:"exposure"`
		} `yaml:"aggregate"`
	}
	require.NoError(t, yaml.Unmarshal(raw, &team))
	perSend, err := strconv.ParseInt(team.Route.ExposurePerSend, 10, 64)
	require.NoError(t, err)
	aggregate, err := strconv.ParseInt(team.Aggregate.Exposure, 10, 64)
	require.NoError(t, err)
	require.Positive(t, perSend)
	require.GreaterOrEqual(t, aggregate, perSend)
	return teamBudget{perSend: perSend, aggregate: aggregate}
}
