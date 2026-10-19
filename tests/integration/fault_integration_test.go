//go:build integration

package integration

import (
	"context"
	"fmt"
	"maps"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/sdk/client"
	batchv1 "k8s.io/api/batch/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

// faultWorkerConfig is the worker configuration of the controlled
// scenarios below: the checked-in file with DEVELOPMENT_ONLY fault
// injection (first completion receipt of the named durable commands lost)
// and short cleanup/reconciliation bounds so an unconfirmed cleanup is
// observed within the test budget. Every value still passes the service's
// own validation.
func faultWorkerConfig(t *testing.T, root, dir string) string {
	t.Helper()
	base, err := os.ReadFile(filepath.Join(root, serviceDirs["anvilkit-agent-workflow"], "config.yaml"))
	require.NoError(t, err)
	text := string(base)
	for _, r := range [][2]string{
		{"control_activity_timeout: 30s", "control_activity_timeout: 10s"},
		{"timeout: 3m", "timeout: 20s"},
		{"max_attempts: 2", "max_attempts: 1"},
		{"unresolved_settle_window: 60s", "unresolved_settle_window: 15s"},
		{"reconcile_initial_interval: 5s", "reconcile_initial_interval: 3s"},
		{"reconcile_max_interval: 1m", "reconcile_max_interval: 10s"},
		{"reconcile_max_duration: 24h", "reconcile_max_duration: 5m"},
	} {
		require.Contains(t, text, r[0], "the checked-in configuration still carries the value this scenario overrides")
		text = strings.Replace(text, r[0], r[1], 1)
	}
	// The checked-in file enables development mode or not; the scenario
	// enables it with its lost receipts either way.
	switch {
	case strings.Contains(text, "development:\n  enabled: false"):
		text = strings.Replace(text, "development:\n  enabled: false", "development:\n  enabled: true\n  lose_receipt_once: [PrepareLaunch, AcceptResult, CloseAttempt]", 1)
	case strings.Contains(text, "development:\n  enabled: true"):
		text = strings.Replace(text, "development:\n  enabled: true", "development:\n  enabled: true\n  lose_receipt_once: [PrepareLaunch, AcceptResult, CloseAttempt]", 1)
	default:
		t.Fatal("the checked-in configuration has no development block this scenario can extend")
	}
	path := filepath.Join(dir, "workflow.fault.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

// unresolvedCreateWorkerConfig is the worker configuration of the
// unresolved-create scenario: the first CreateJob really creates the Job
// but its result is withheld until the Workflow cancels the Activity (the
// worker learns of the cancel through its throttled heartbeat, about 30 s),
// and the first completion of each cleanup Activity is dropped. The
// create's bound and the observation window are widened so the held
// request outlives the heartbeat latency; every value still passes the
// service's own validation.
func unresolvedCreateWorkerConfig(t *testing.T, root, dir string) string {
	t.Helper()
	base, err := os.ReadFile(filepath.Join(root, serviceDirs["anvilkit-agent-workflow"], "config.yaml"))
	require.NoError(t, err)
	text := string(base)
	for _, r := range [][2]string{
		{"control_activity_timeout: 30s", "control_activity_timeout: 60s"},
		{"timeout: 3m", "timeout: 90s"},
		{"reconcile_initial_interval: 5s", "reconcile_initial_interval: 3s"},
		{"reconcile_max_interval: 1m", "reconcile_max_interval: 10s"},
		{"reconcile_max_duration: 24h", "reconcile_max_duration: 5m"},
	} {
		require.Contains(t, text, r[0], "the checked-in configuration still carries the value this scenario overrides")
		text = strings.Replace(text, r[0], r[1], 1)
	}
	// As in faultWorkerConfig: the checked-in file enables development mode
	// or not (enabled since P0.1); the scenario extends it with its faults.
	const faults = "development:\n  enabled: true\n  lose_receipt_once: [ObserveLaunch, RegisterInstance, DeleteJob]\n  hold_until_canceled_once: [CreateJob]"
	switch {
	case strings.Contains(text, "development:\n  enabled: false"):
		text = strings.Replace(text, "development:\n  enabled: false", faults, 1)
	case strings.Contains(text, "development:\n  enabled: true"):
		text = strings.Replace(text, "development:\n  enabled: true", faults, 1)
	default:
		t.Fatal("the checked-in configuration has no development block this scenario can extend")
	}
	path := filepath.Join(dir, "workflow.unresolved-create.yaml")
	require.NoError(t, os.WriteFile(path, []byte(text), 0o600))
	return path
}

func (e *env) workerLog() string {
	raw, err := os.ReadFile(filepath.Join(e.local, "workflow.log"))
	require.NoError(e.t, err)
	return string(raw)
}

// launchIdentity reads the durable launch obligation Control committed for
// the operation: the identities a duplicate Pod would have to carry.
func (e *env) launchIdentity(operationID string) (launchID, attemptID, launchKey, image string) {
	conn := e.db()
	require.Eventually(e.t, func() bool {
		err := conn.QueryRow(context.Background(),
			"SELECT l.launch_id, l.attempt_id, l.launch_key, l.image_digest FROM launches l WHERE l.operation_id = $1", operationID).Scan(&launchID, &attemptID, &launchKey, &image)
		return err == nil
	}, 30*time.Second, 100*time.Millisecond, "the launch obligation is committed")
	return
}

// createRequestAnnotation is the launcher's create marker on a Job and its
// Pod template; the namespace's admission policy
// (deploy/policies/chart/templates/components-jobs.yaml) admits no Job
// without it on both.
const createRequestAnnotation = "anvilkit.io/create-request"

// duplicateJob is the controlled simulation of a duplicate physical Pod on
// the real backend: a second Job in the namespace whose Pod carries the
// launch-key label and the fixture's identities. The launcher identity can
// create Jobs only (the launcher identity rendered from the Workflow chart by deploy/dev/up.sh), which is why
// the duplicate arrives through a Job rather than a bare Pod. The duplicate
// is the trusted original's own launcher labels, create-request marker and
// Pod template under another name — what the admission policy requires of
// every Job in the namespace — and none of the server-assigned identity
// (UID, resourceVersion, the generated selector and its labels); every map
// is copied so nothing here mutates the original object.
func (e *env) duplicateJob(operationID string) string {
	cs := e.k8s()
	launchID, attemptID, launchKey, image := e.launchIdentity(operationID)
	var original *batchv1.Job
	require.Eventually(e.t, func() bool {
		job, err := cs.BatchV1().Jobs("anvilkit-components").Get(context.Background(), launchKey, metav1.GetOptions{})
		if err != nil {
			return false
		}
		original = job
		return true
	}, 60*time.Second, 100*time.Millisecond, "the original Job exists under the launch key")
	spec := original.Spec.DeepCopy()
	spec.Selector, spec.ManualSelector = nil, nil
	for _, generated := range []string{"batch.kubernetes.io/controller-uid", "batch.kubernetes.io/job-name", "controller-uid", "job-name"} {
		delete(spec.Template.Labels, generated)
	}
	dup := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: launchKey + "-dup", Namespace: original.Namespace, Labels: maps.Clone(original.Labels), Annotations: maps.Clone(original.Annotations)},
		Spec:       *spec,
	}
	require.NotEmpty(e.t, dup.Annotations[createRequestAnnotation], "the original Job carries the launcher's create marker")
	require.Equal(e.t, dup.Annotations[createRequestAnnotation], dup.Spec.Template.Annotations[createRequestAnnotation], "the duplicate keeps the original create marker on the Job and its template")
	require.Equal(e.t, launchKey, dup.Labels["anvilkit.io/launch-key"])
	require.Equal(e.t, attemptID, dup.Labels["anvilkit.io/attempt-id"])
	_, imageDigest, _ := strings.Cut(dup.Spec.Template.Spec.Containers[0].Image, "@")
	require.Equal(e.t, image, imageDigest, "the duplicate runs the pinned fixture image")
	require.Equal(e.t, launchID, envValue(dup, "ANVILKIT_LAUNCH_ID"))
	require.Equal(e.t, attemptID, envValue(dup, "ANVILKIT_ATTEMPT_ID"))
	_, err := cs.BatchV1().Jobs("anvilkit-components").Create(context.Background(), dup, metav1.CreateOptions{})
	require.NoError(e.t, err, "the duplicate Job is admitted under the namespace's policy")
	require.Contains(e.t, original.Spec.Template.Labels, "batch.kubernetes.io/job-name", "the original object was not mutated by the copy")
	e.t.Cleanup(func() {
		_ = cs.BatchV1().Jobs("anvilkit-components").Delete(context.Background(), dup.Name, metav1.DeleteOptions{PropagationPolicy: ptr.To(metav1.DeletePropagationForeground)})
	})
	return dup.Name
}

func envValue(job *batchv1.Job, name string) string {
	for _, ev := range job.Spec.Template.Spec.Containers[0].Env {
		if ev.Name == name {
			return ev.Value
		}
	}
	return ""
}

func (e *env) deleteJob(name string) {
	cs := e.k8s()
	require.NoError(e.t, cs.BatchV1().Jobs("anvilkit-components").Delete(context.Background(), name, metav1.DeleteOptions{PropagationPolicy: ptr.To(metav1.DeletePropagationForeground)}))
}

// TestFaultScenarios runs the P05 failure cases that need controlled
// injection on the real development foundation (PostgreSQL, Temporal,
// kind): lost completion receipts inside Temporal and duplicate Pods with
// an unconfirmed cleanup that the run reconciles until the evidence
// settles the operation and its cancel command.
func TestFaultScenarios(t *testing.T) {
	root := repoRoot(t)
	e := setupEnv(t, false, faultWorkerConfig(t, root, t.TempDir()))
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Minute)
	defer cancel()

	t.Run("lost completion receipts of PrepareLaunch, AcceptResult and CloseAttempt reenter the same commands: one launch, one result, one close", func(t *testing.T) {
		status, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-receipt"))
		require.Equal(t, 202, status, "%v", out)
		opID := out["operationId"].(string)
		frames := e.stream(ctx, opID, tokenA, "", terminal)
		require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))
		require.Equal(t, []string{"intake", "accepted", "scheduled", "attempt_open", "launch_prepared", "running", "result_accepted", "closed"}, phases(frames),
			"a lost receipt adds no projection change: the retried command returned the original record")
		e.assertSingleAcceptedResult(opID)
		e.assertJobGone(opID)
		attempts, launches, instances, stages := e.counts(opID)
		require.Equal(t, [4]int{1, 1, 1, 1}, [4]int{attempts, launches, instances, stages})
		log := e.workerLog()
		for _, name := range []string{"PrepareLaunch", "AcceptResult", "CloseAttempt"} {
			require.Equal(t, 1, strings.Count(log, `"activity":"`+name+`"`), "the receipt of %s was dropped exactly once (worker log)", name)
		}
		var closes int
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT count(*) FROM operation_events WHERE operation_id = $1 AND transition_id LIKE 'attempt:%:closed'", opID).Scan(&closes))
		require.Equal(t, 1, closes, "the retried close returned the original close, it did not close twice")
	})

	t.Run("duplicate Pod on the backend: one current instance owns the only result; the lingering duplicate keeps cleanup unknown until it is gone, then the run settles", func(t *testing.T) {
		status, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-dup-pod"))
		require.Equal(t, 202, status, "%v", out)
		opID := out["operationId"].(string)
		e.stream(ctx, opID, tokenA, "", func(f frame) bool { return phase(f) == "launch_prepared" || terminal(f) })
		dupName := e.duplicateJob(opID)

		// The attempt closes with unknown cleanup: the duplicate's Pod still
		// carries the launch key, so the launcher cannot confirm the launch
		// stopped; the projection shows reconciling and new dispatch is fenced.
		frames := e.stream(ctx, opID, tokenA, "", func(f frame) bool { return lifecycle(f) == "reconciling" || terminal(f) })
		last := frames[len(frames)-1]
		require.Equal(t, "reconciling", lifecycle(last), phases(frames))
		require.Equal(t, "closed", phase(last))
		require.Equal(t, "unknown", last.Data["payload"].(map[string]any)["cleanup"])
		attempts, launches, instances, stages := e.counts(opID)
		require.Equal(t, 1, attempts)
		require.Equal(t, 1, launches)
		require.Equal(t, 2, instances, "both Pods were registered under the one launch")
		require.Equal(t, 1, stages, "exactly one accepted result")
		var current int
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT count(*) FROM physical_instances i JOIN attempts a ON a.attempt_id = i.attempt_id WHERE a.operation_id = $1 AND i.is_current", opID).Scan(&current))
		require.Equal(t, 1, current, "exactly one current physical owner")
		var stageInstanceCurrent bool
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT i.is_current FROM stage_manifests s JOIN physical_instances i ON i.instance_id = s.instance_id WHERE s.operation_id = $1", opID).Scan(&stageInstanceCurrent))
		require.True(t, stageInstanceCurrent, "the result belongs to the current instance")

		// A cancel during reconciliation is tracked but stays pending: the
		// senders are not known to have stopped.
		revision, _ := last.Data["revision"].(string)
		status, receipt := e.call("POST", "/api/v1/operations/"+opID+"/commands", tokenA, `{"commandId":"cmd-`+run+`-dup-cancel","kind":"cancel","expectedRevision":"`+revision+`"}`)
		require.Equal(t, 202, status, "%v", receipt)
		require.Equal(t, "pending", receipt["outcome"])

		// The Workflow run is still the recovery owner: once the duplicate is
		// gone, its next reconciliation round confirms the launch stopped and
		// the settlement applies the pending cancel under the original
		// attempt; no new attempt, launch or result appears.
		e.deleteJob(dupName)
		frames = e.stream(ctx, opID, tokenA, last.ID, terminal)
		final := frames[len(frames)-1]
		require.Equal(t, "canceled", lifecycle(final), phases(frames))
		require.Equal(t, "complete", final.Data["payload"].(map[string]any)["cleanup"])
		require.Equal(t, "cancel_applied", final.Data["payload"].(map[string]any)["control"])
		e.assertJobGone(opID)
		attempts, launches, instances, stages = e.counts(opID)
		require.Equal(t, [4]int{1, 1, 2, 1}, [4]int{attempts, launches, instances, stages}, "settlement added no attempt, launch or result")
		var outcome string
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT outcome FROM operation_commands WHERE command_id = $1", "cmd-"+run+"-dup-cancel").Scan(&outcome))
		require.Equal(t, "applied", outcome, "the pending cancel settled with the evidence")
		var events int
		require.NoError(t, e.db().QueryRow(context.Background(), "SELECT count(*) FROM operation_events WHERE operation_id = $1 AND transition_id LIKE 'attempt:%:cleanup-settled'", opID).Scan(&events))
		require.Equal(t, 1, events, "one settlement transition")
		var wfClosed bool
		require.Eventually(t, func() bool {
			wfClosed = e.workflowClosed(opID)
			return wfClosed
		}, time.Minute, time.Second, "the Workflow run completes after the settlement")
	})

	t.Run("duplicate Pod without a cancel: the settlement lands on succeeded with the one result", func(t *testing.T) {
		status, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-dup-pod-2"))
		require.Equal(t, 202, status, "%v", out)
		opID := out["operationId"].(string)
		e.stream(ctx, opID, tokenA, "", func(f frame) bool { return phase(f) == "launch_prepared" || terminal(f) })
		dupName := e.duplicateJob(opID)
		frames := e.stream(ctx, opID, tokenA, "", func(f frame) bool { return lifecycle(f) == "reconciling" || terminal(f) })
		last := frames[len(frames)-1]
		require.Equal(t, "reconciling", lifecycle(last), phases(frames))
		e.deleteJob(dupName)
		frames = e.stream(ctx, opID, tokenA, last.ID, terminal)
		final := frames[len(frames)-1]
		require.Equal(t, "succeeded", lifecycle(final), phases(frames))
		require.Equal(t, "complete", final.Data["payload"].(map[string]any)["cleanup"])
		require.NotContains(t, final.Data["payload"], "failureCode", "the uncertainty code is cleared")
		e.assertSingleAcceptedResult(opID)
		e.assertJobGone(opID)
	})
}

// workflowClosed reports whether the operation's Workflow run has closed
// (completed, failed or canceled) in Temporal.
func (e *env) workflowClosed(operationID string) bool {
	tc, err := client.Dial(client.Options{HostPort: e.temporal, Namespace: "anvilkit"})
	require.NoError(e.t, err)
	defer tc.Close()
	desc, err := tc.DescribeWorkflowExecution(context.Background(), operationID, "")
	if err != nil {
		return false
	}
	return desc.GetWorkflowExecutionInfo().GetStatus() != enumspb.WORKFLOW_EXECUTION_STATUS_RUNNING
}

// scheduledActivities counts the Activity types scheduled in the
// operation's Workflow history.
func scheduledActivities(t *testing.T, tc client.Client, operationID string) map[string]int {
	t.Helper()
	out := map[string]int{}
	iter := tc.GetWorkflowHistory(context.Background(), operationID, "", false, enumspb.HISTORY_EVENT_FILTER_TYPE_ALL_EVENT)
	for iter.HasNext() {
		ev, err := iter.Next()
		require.NoError(t, err)
		if attrs := ev.GetActivityTaskScheduledEventAttributes(); attrs != nil {
			out[attrs.GetActivityType().GetName()]++
		}
	}
	return out
}

// TestUnresolvedCreateRecovery is the controlled simulation, on the real
// development foundation, of a cancel that arrives while the Job create
// request is in flight and lands anyway, followed by lost completion
// receipts of every cleanup Activity (worker gone after the call): the
// run must still confirm the cleanup under the original launch identity
// and settle the pending cancel, without a replacement launch.
func TestUnresolvedCreateRecovery(t *testing.T) {
	root := repoRoot(t)
	e := setupEnv(t, false, unresolvedCreateWorkerConfig(t, root, t.TempDir()))
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Minute)
	defer cancel()
	tc, err := client.Dial(client.Options{HostPort: e.temporal, Namespace: "anvilkit"})
	require.NoError(t, err)
	defer tc.Close()

	status, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-unresolved"))
	require.Equal(t, 202, status, "%v", out)
	opID := out["operationId"].(string)
	var revision string
	e.stream(ctx, opID, tokenA, "", func(f frame) bool {
		if phase(f) == "launch_prepared" {
			revision, _ = f.Data["revision"].(string)
			return true
		}
		return terminal(f)
	})
	require.NotEmpty(t, revision, "operation reached the launch")
	// The create request is held on the worker while the Job it created is
	// already running on the cluster.
	_, _, launchKey, _ := e.launchIdentity(opID)
	cs := e.k8s()
	require.Eventually(t, func() bool {
		_, err := cs.BatchV1().Jobs("anvilkit-components").Get(ctx, launchKey, metav1.GetOptions{})
		return err == nil
	}, 60*time.Second, 200*time.Millisecond, "the held create landed on the cluster")
	require.Eventually(t, func() bool {
		return strings.Contains(e.workerLog(), `"activity":"CreateJob"`)
	}, 30*time.Second, 200*time.Millisecond, "the worker withholds the create's result")

	status, receipt := e.call("POST", "/api/v1/operations/"+opID+"/commands", tokenA, `{"commandId":"cmd-`+run+`-unresolved-cancel","kind":"cancel","expectedRevision":"`+revision+`"}`)
	require.Equal(t, 202, status, "%v", receipt)
	require.Equal(t, "pending", receipt["outcome"], "the launch is not known to have stopped")

	frames := e.stream(ctx, opID, tokenA, "", terminal)
	final := frames[len(frames)-1]
	require.Equal(t, "canceled", lifecycle(final), phases(frames))
	require.Equal(t, "complete", final.Data["payload"].(map[string]any)["cleanup"], "the cancel settled only with confirmed cleanup")
	require.Equal(t, "cancel_applied", final.Data["payload"].(map[string]any)["control"])
	e.assertJobGone(opID)

	attempts, launches, instances, stages := e.counts(opID)
	require.Equal(t, 1, attempts)
	require.Equal(t, 1, launches, "no replacement launch")
	require.GreaterOrEqual(t, instances, 1, "the Pod observed during cleanup is registered under the original launch")
	require.Equal(t, 0, stages, "a canceled launch accepts no result")
	var current int
	require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM physical_instances i JOIN attempts a ON a.attempt_id = i.attempt_id WHERE a.operation_id = $1 AND i.is_current", opID).Scan(&current))
	require.Equal(t, 1, current)
	var outcome string
	require.NoError(t, e.db().QueryRow(ctx, "SELECT outcome FROM operation_commands WHERE command_id = $1", "cmd-"+run+"-unresolved-cancel").Scan(&outcome))
	require.Equal(t, "applied", outcome)
	var closes int
	require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM operation_events WHERE operation_id = $1 AND transition_id LIKE 'attempt:%:closed'", opID).Scan(&closes))
	require.Equal(t, 1, closes)

	log := e.workerLog()
	require.Equal(t, 1, strings.Count(log, `"activity":"CreateJob"`), "the create's result was withheld exactly once")
	for _, name := range []string{"ObserveLaunch", "RegisterInstance", "DeleteJob"} {
		require.Equal(t, 1, strings.Count(log, `"activity":"`+name+`"`), "the receipt of %s was dropped exactly once (worker log)", name)
	}
	scheduled := scheduledActivities(t, tc, opID)
	require.Equal(t, 1, scheduled["CreateJob"], "one create under the original launch key, never a replacement")
	require.GreaterOrEqual(t, scheduled["ObserveLaunch"], 1)
	require.GreaterOrEqual(t, scheduled["RegisterInstance"], 1)
	require.Equal(t, 0, scheduled["ObserveJob"], "a canceled create is never observed as a running launch")
	require.Eventually(t, func() bool { return e.workflowClosed(opID) }, time.Minute, time.Second, "the Workflow run completes after the settlement")
}
