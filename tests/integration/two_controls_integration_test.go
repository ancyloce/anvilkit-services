//go:build integration

package integration

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	enumspb "go.temporal.io/api/enums/v1"
	"go.temporal.io/api/workflowservice/v1"
	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/converter"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
)

// canonicalDigest reproduces the API's command digest (canonical JSON of the
// decoded body) so a request can be replayed to Control directly under the
// identity the API derived.
func canonicalDigest(t *testing.T, body string) string {
	t.Helper()
	var decoded any
	dec := json.NewDecoder(strings.NewReader(body))
	dec.UseNumber()
	require.NoError(t, dec.Decode(&decoded))
	canonical, err := json.Marshal(decoded)
	require.NoError(t, err)
	return fmt.Sprintf("sha256:%x", sha256.Sum256(canonical))
}

// dialControl connects as the given workload (P0.1): every client presents
// the certificate of the role it simulates.
func (e *env) dialControl(addr, principal string) *grpc.ClientConn {
	return dialControlAs(e.t, addr, principal)
}

func (e *env) counts(operationID string) (attempts, launches, instances, stages int) {
	conn := e.db()
	ctx := context.Background()
	require.NoError(e.t, conn.QueryRow(ctx, "SELECT count(*) FROM attempts WHERE operation_id = $1", operationID).Scan(&attempts))
	require.NoError(e.t, conn.QueryRow(ctx, "SELECT count(*) FROM launches WHERE operation_id = $1", operationID).Scan(&launches))
	require.NoError(e.t, conn.QueryRow(ctx, "SELECT count(*) FROM physical_instances i JOIN attempts a ON a.attempt_id = i.attempt_id WHERE a.operation_id = $1", operationID).Scan(&instances))
	require.NoError(e.t, conn.QueryRow(ctx, "SELECT count(*) FROM stage_manifests WHERE operation_id = $1", operationID).Scan(&stages))
	return
}

// inventoryObjects lists the published obligation objects of an operation.
func (e *env) inventoryObjects(operationID, launchID string) []string {
	var found []string
	for _, key := range []string{"intake/" + operationID, "job-launch/" + launchID} {
		if _, err := os.Stat(filepath.Join(e.local, "inventory", key)); err == nil {
			found = append(found, key)
		}
	}
	entries, _ := filepath.Glob(filepath.Join(e.local, "inventory", "*", "*.partial-*"))
	require.Empty(e.t, entries, "no interrupted inventory write is left behind")
	return found
}

// TestTwoControlProcesses runs two real Control binaries on one database and
// one inventory directory: the API submits and observes through A, the
// Workflow executes through B, both relays poll the same intents.
func TestTwoControlProcesses(t *testing.T) {
	e := setupTwoControls(t)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	execA := controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	opsB := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddrB, principalAPI))
	tc, err := client.Dial(client.Options{HostPort: e.temporal, Namespace: "anvilkit"})
	require.NoError(t, err)
	defer tc.Close()

	t.Run("the same submission on both processes converges and both relays start exactly one workflow", func(t *testing.T) {
		body := createBody("cmd-" + run + "-two")
		digest := canonicalDigest(t, body)
		var (
			wg      sync.WaitGroup
			apiOut  map[string]any
			apiCode int
			grpcOut *controlv1.CreateOperationResponse
			grpcErr error
		)
		wg.Add(2)
		go func() { defer wg.Done(); apiCode, apiOut = e.call("POST", "/api/v1/operations", tokenA, body) }()
		go func() {
			defer wg.Done()
			grpcOut, grpcErr = opsB.CreateOperation(ctx, &controlv1.CreateOperationRequest{
				Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "cmd-" + run + "-two", ActorId: "user_a", RequestDigest: digest},
				Scope:   &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"},
				Kind:    controlv1.OperationKind_OPERATION_KIND_LOCAL_CHECK,
				Subject: &controlv1.OperationSubject{ProfileId: "local-check-v1", SubjectDigest: subjectDigest},
			})
		}()
		wg.Wait()
		require.Equal(t, 202, apiCode, "%v", apiOut)
		require.NoError(t, grpcErr)
		opID := apiOut["operationId"].(string)
		require.Equal(t, opID, grpcOut.GetOperation().GetOperationId(), "one operation for one command identity across processes")
		var ops int
		require.NoError(t, e.db().QueryRow(ctx, "SELECT count(*) FROM operations WHERE command_id = $1", "cmd-"+run+"-two").Scan(&ops))
		require.Equal(t, 1, ops)

		frames := e.stream(ctx, opID, tokenA, "", terminal)
		require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))
		e.assertSingleAcceptedResult(opID)
		e.assertJobGone(opID)

		// Two relays, one Temporal history: a single run for the operation id.
		listed, err := tc.ListWorkflow(ctx, &workflowservice.ListWorkflowExecutionsRequest{Query: fmt.Sprintf("WorkflowId = '%s'", opID)})
		require.NoError(t, err)
		require.Len(t, listed.GetExecutions(), 1, "exactly one workflow run for the operation")
		var relayRun string
		require.NoError(t, e.db().QueryRow(ctx, "SELECT coalesce(relay_run_id, '') FROM operations WHERE operation_id = $1", opID).Scan(&relayRun))
		require.Equal(t, listed.GetExecutions()[0].GetExecution().GetRunId(), relayRun, "the recorded relay run is the only run")
	})

	t.Run("commands committed by B are reentered on A after a lost acknowledgement without duplicates", func(t *testing.T) {
		code, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-replay"))
		require.Equal(t, 202, code, "%v", out)
		opID := out["operationId"].(string)
		frames := e.stream(ctx, opID, tokenA, "", final)
		require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))
		require.Equal(t, "finance:settled", frames[len(frames)-1].Data["transitionId"], "the stream ends after the operation's last event")
		attempts, launches, instances, stages := e.counts(opID)
		require.Equal(t, [3]int{1, 1, 1}, [3]int{attempts, launches, stages})
		require.GreaterOrEqual(t, instances, 1)

		conn := e.db()
		var attemptID, openCmd, openDigest, stepID, profileID string
		var visit int64
		require.NoError(t, conn.QueryRow(ctx, "SELECT attempt_id, command_id, request_digest, step_id, visit_ordinal, profile_id FROM attempts WHERE operation_id = $1", opID).
			Scan(&attemptID, &openCmd, &openDigest, &stepID, &visit, &profileID))
		var launchID, launchCmd, launchDigest, key, backend, image string
		var deadline time.Time
		require.NoError(t, conn.QueryRow(ctx, "SELECT launch_id, command_id, request_digest, launch_key, backend, image_digest, deadline FROM launches WHERE operation_id = $1", opID).
			Scan(&launchID, &launchCmd, &launchDigest, &key, &backend, &image, &deadline))
		var stageID, stageCmd, stageDigest, instanceID, resultDigest, observer string
		var storedManifest []byte
		require.NoError(t, conn.QueryRow(ctx, "SELECT stage_id, command_id, request_digest, instance_id, result_digest, observer_identity, result_manifest FROM stage_manifests WHERE operation_id = $1", opID).
			Scan(&stageID, &stageCmd, &stageDigest, &instanceID, &resultDigest, &observer, &storedManifest))
		require.Len(t, e.inventoryObjects(opID, launchID), 2, "intake and job-launch obligations are published once")
		// The retrying caller resends its original input, which Temporal
		// keeps in the history; the jsonb copy in Control is normalized and
		// does not reproduce the accepted bytes (recorded P08 input).
		manifest := acceptResultManifest(t, tc, opID)
		require.Equal(t, resultDigest, digestOf(string(manifest)), "the Activity input hashes to the accepted result digest")
		require.NotEqual(t, resultDigest, digestOf(string(storedManifest)), "the jsonb copy is normalized, not byte-exact")

		// The Workflow's replies from B are "lost"; the identical commands
		// reach A. Every one returns the original record and writes nothing.
		cmd := func(id, digest string) *controlv1.CommandIdentity {
			return &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: id, ActorId: "anvilkit-agent-workflow", RequestDigest: digest}
		}
		opened, err := execA.OpenAttempt(ctx, &controlv1.OpenAttemptRequest{Command: cmd(openCmd, openDigest), OperationId: opID, StepId: stepID, VisitOrdinal: fmt.Sprint(visit), ProfileId: profileID})
		require.NoError(t, err)
		require.True(t, opened.GetExisting())
		require.Equal(t, attemptID, opened.GetAttempt().GetAttemptId())
		launched, err := execA.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{Command: cmd(launchCmd, launchDigest), AttemptId: attemptID, LaunchKey: key, Backend: backend, ImageDigest: image, Deadline: timestamppb.New(deadline)})
		require.NoError(t, err)
		require.True(t, launched.GetExisting())
		require.Equal(t, launchID, launched.GetLaunchId())
		accepted, err := execA.AcceptResult(ctx, &controlv1.AcceptResultRequest{Command: cmd(stageCmd, stageDigest), AttemptId: attemptID, InstanceId: instanceID, ProfileId: profileID,
			Verdict: controlv1.Verdict_VERDICT_CERTIFIED, ResultDigest: resultDigest, ResultManifest: manifest, ObserverIdentity: observer})
		require.NoError(t, err)
		require.True(t, accepted.GetExisting())
		require.Equal(t, stageID, accepted.GetStage().GetStageId())
		closed, err := execA.CloseAttempt(ctx, &controlv1.CloseAttemptRequest{Command: cmd(attemptID+":close", digestOf("close")), AttemptId: attemptID,
			Outcome: controlv1.AttemptOutcome_ATTEMPT_OUTCOME_COMPLETED, Cleanup: controlv1.CleanupState_CLEANUP_STATE_COMPLETE})
		require.NoError(t, err)
		require.True(t, closed.GetExisting())
		require.Equal(t, controlv1.Lifecycle_LIFECYCLE_SUCCEEDED, closed.GetOperation().GetLifecycle())

		// A changed request under a committed identity is a conflict, never a second launch.
		_, err = execA.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{Command: cmd(launchCmd, digestOf("changed")), AttemptId: attemptID, LaunchKey: key, Backend: backend, ImageDigest: image, Deadline: timestamppb.New(deadline)})
		require.Equal(t, codes.Aborted, status.Code(err))
		// Nothing launches after the terminal state: the operation is fenced.
		_, err = execA.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{Command: cmd(launchCmd+"-2", digestOf("late")), AttemptId: attemptID, LaunchKey: key + "-2", Backend: backend, ImageDigest: image, Deadline: timestamppb.New(time.Now().Add(time.Minute))})
		require.Equal(t, codes.FailedPrecondition, status.Code(err))

		attempts, launches, _, stages = e.counts(opID)
		require.Equal(t, [3]int{1, 1, 1}, [3]int{attempts, launches, stages}, "replays wrote nothing")
		require.Len(t, e.inventoryObjects(opID, launchID), 2)
		var events int
		require.NoError(t, conn.QueryRow(ctx, "SELECT count(*) FROM operation_events WHERE operation_id = $1", opID).Scan(&events))
		require.Equal(t, frames[len(frames)-1].ID, fmt.Sprint(events), "no event was appended by the replays")
	})

	t.Run("Control A killed while the consumer streams: B completes the chain once, the consumer resumes from its cursor", func(t *testing.T) {
		code, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-killa"))
		require.Equal(t, 202, code, "%v", out)
		opID := out["operationId"].(string)
		var last string
		first := e.stream(ctx, opID, tokenA, "", func(f frame) bool {
			last = f.ID
			if phase(f) == "launch_prepared" {
				e.kill("control")
				return false // keep reading until the stream ends with A
			}
			return terminal(f)
		})
		require.NotEmpty(t, first)
		require.False(t, terminal(first[len(first)-1]), "the stream ended when A died, before the operation was terminal")
		e.start("control")
		e.waitTCP(e.controlAddr)
		resumed := e.stream(ctx, opID, tokenA, last, terminal)
		require.NotEmpty(t, resumed)
		require.Greater(t, resumed[0].ID, last, "replay continues after the cursor")
		require.Equal(t, "succeeded", lifecycle(resumed[len(resumed)-1]), phases(resumed))
		e.assertSingleAcceptedResult(opID)
		e.assertJobGone(opID)
	})

	t.Run("Control B killed after the launch obligation is committed: the retried Activities reenter the original launch", func(t *testing.T) {
		code, out := e.call("POST", "/api/v1/operations", tokenA, createBody("cmd-"+run+"-killb"))
		require.Equal(t, 202, code, "%v", out)
		opID := out["operationId"].(string)
		conn := e.db()
		deadline := time.Now().Add(3 * time.Minute)
		var launchID string
		for launchID == "" {
			require.True(t, time.Now().Before(deadline), "launch obligation never committed")
			_ = conn.QueryRow(ctx, "SELECT launch_id FROM launches WHERE operation_id = $1", opID).Scan(&launchID)
			time.Sleep(20 * time.Millisecond)
		}
		e.kill("control-b") // the Workflow's Activity may or may not have received its reply
		time.Sleep(2 * time.Second)
		e.start("control-b")
		e.waitTCP(e.controlAddrB)
		frames := e.stream(ctx, opID, tokenA, "", terminal)
		require.Equal(t, "succeeded", lifecycle(frames[len(frames)-1]), phases(frames))
		attempts, launches, _, stages := e.counts(opID)
		require.Equal(t, [3]int{1, 1, 1}, [3]int{attempts, launches, stages}, "the original identities were reentered, nothing was duplicated")
		require.Len(t, e.inventoryObjects(opID, launchID), 2)
		e.assertJobGone(opID)
	})
}

func digestOf(s string) string { return fmt.Sprintf("sha256:%x", sha256.Sum256([]byte(s))) }

// acceptResultManifest reads the AcceptResult Activity input of the
// operation's workflow from the Temporal history: the manifest bytes the
// Workflow would resend when its reply was lost.
func acceptResultManifest(t *testing.T, tc client.Client, operationID string) []byte {
	t.Helper()
	iter := tc.GetWorkflowHistory(context.Background(), operationID, "", false, enumspb.HISTORY_EVENT_FILTER_TYPE_ALL_EVENT)
	for iter.HasNext() {
		ev, err := iter.Next()
		require.NoError(t, err)
		attrs := ev.GetActivityTaskScheduledEventAttributes()
		if attrs == nil || attrs.GetActivityType().GetName() != "AcceptResult" {
			continue
		}
		var in struct {
			Verdict struct {
				Manifest []byte
			}
		}
		require.NoError(t, converter.GetDefaultDataConverter().FromPayloads(attrs.GetInput(), &in))
		require.NotEmpty(t, in.Verdict.Manifest)
		return in.Verdict.Manifest
	}
	t.Fatal("no AcceptResult activity in the workflow history")
	return nil
}
