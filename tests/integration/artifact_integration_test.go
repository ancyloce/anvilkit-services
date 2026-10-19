//go:build integration

package integration

import (
	"archive/zip"
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	controlv1 "github.com/ancyloce/anvilkit-agent-contracts/go/anvilkit/control/v1"
	"github.com/ancyloce/anvilkit-agent-contracts/go/jobschema"
)

// upload performs the one request a transfer capability authorizes, as the
// API's client would: the method and URL as given, the signed headers as
// given, the body as declared. It returns the object version the backend
// assigned.
func upload(t *testing.T, out map[string]any, body []byte) (string, int) {
	t.Helper()
	url, _ := out["uploadCapability"].(string)
	method, _ := out["uploadMethod"].(string)
	require.NotEmpty(t, url)
	require.Equal(t, "PUT", method)
	req, err := http.NewRequest(method, url, bytes.NewReader(body))
	require.NoError(t, err)
	headers, _ := out["uploadHeaders"].(map[string]any)
	for k, v := range headers {
		if strings.EqualFold(k, "Content-Length") {
			continue
		}
		req.Header.Set(k, fmt.Sprint(v))
	}
	req.ContentLength = int64(len(body))
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.Header.Get("x-amz-version-id"), resp.StatusCode
}

func zipBytes(t *testing.T, entries map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for name, body := range entries {
		f, err := w.Create(name)
		require.NoError(t, err)
		_, err = f.Write([]byte(body))
		require.NoError(t, err)
	}
	require.NoError(t, w.Close())
	return buf.Bytes()
}

// TestArtifactTransfer runs API-12 end to end on the development
// foundation: the API begins a scoped transfer through Control, the client
// uploads through the capability to the foundation's MinIO (a versioned
// S3-compatible bucket, DEVELOPMENT_ONLY), Control finalizes on the bytes
// it reads back by version, and every refusal is observed through the
// public surface. Nothing here qualifies the production object backend.
func TestArtifactTransfer(t *testing.T) {
	if os.Getenv("ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT") == "" {
		skipMissing(t, "ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT", "not set; source .local/dev/env.sh after deploy/dev/up.sh")
	}
	e := setup(t)
	run := fmt.Sprint(time.Now().UnixNano())
	body := []byte("a prompt for run " + run + "\n")
	begin := func(id, class, mediaType string, data []byte) string {
		return fmt.Sprintf(`{"commandId":"xfer-%s-%s","class":"%s","expectedDigest":"%s","expectedSize":"%d","mediaType":"%s"}`, run, id, class, digestOf(string(data)), len(data), mediaType)
	}

	code, out := e.call("POST", "/api/v1/artifacts/transfers", tokenA, begin("p", "prompt", "text/plain", body))
	require.Equal(t, 201, code, "%v", out)
	handle, _ := out["handle"].(string)
	require.Equal(t, "begun", out["state"])
	// The transfer record itself names no object key or bucket; only the
	// signed capability (the trusted caller's one upload authorization)
	// carries the object it authorizes.
	record := map[string]any{}
	for k, v := range out {
		if k != "uploadCapability" {
			record[k] = v
		}
	}
	require.NotContains(t, fmt.Sprint(record), "tenant_a/prompt/", "no object key on the public record")
	require.NotContains(t, fmt.Sprint(record), "anvilkit-artifacts", "no bucket on the public record")
	require.Contains(t, fmt.Sprint(out["uploadHeaders"]), "Content-Length", "the signed headers are named for the client")
	code, same := e.call("POST", "/api/v1/artifacts/transfers", tokenA, begin("p", "prompt", "text/plain", body))
	require.Equal(t, 201, code)
	require.Equal(t, out["transferId"], same["transferId"], "the same command returns the original transfer")

	// Finalizing before the upload: the object version does not exist and
	// the transfer stays begun.
	code, fail := e.call("POST", "/api/v1/artifacts/"+handle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-p","objectVersion":"no-such-version"}`)
	require.Equal(t, 400, code, "%v", fail)
	// The capability admits exactly the declared bytes.
	_, status := upload(t, out, append(body, '!'))
	require.NotEqual(t, 200, status, "the signed capability refuses other bytes than declared")
	version, status := upload(t, out, body)
	require.Equal(t, 200, status)
	require.NotEmpty(t, version, "the versioned bucket assigns an object version")

	code, other := e.call("POST", "/api/v1/artifacts/"+handle+"/finalizations", tokenB, `{"commandId":"fin-`+run+`-p","objectVersion":"`+version+`"}`)
	require.Equal(t, 404, code, "another tenant's finalize is not found: %v", other)
	code, fin := e.call("POST", "/api/v1/artifacts/"+handle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-p","objectVersion":"`+version+`"}`)
	require.Equal(t, 200, code, "%v", fin)
	require.Equal(t, "finalized", fin["state"])
	require.Equal(t, version, fin["objectVersion"])
	require.Nil(t, fin["uploadCapability"])
	code, again := e.call("POST", "/api/v1/artifacts/"+handle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-p","objectVersion":"`+version+`"}`)
	require.Equal(t, 200, code)
	require.Equal(t, fin["transferId"], again["transferId"], "a repeated finalize returns the original outcome")
	code, conflict := e.call("POST", "/api/v1/artifacts/"+handle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-p2","objectVersion":"`+version+`"}`)
	require.Equal(t, 409, code, "another command cannot claim the finalization: %v", conflict)

	// The finalized transfer's verified facts are what Control committed.
	conn := e.db()
	var state, actualDigest, objectVersion string
	var actualSize int64
	require.NoError(t, conn.QueryRow(context.Background(), "SELECT state, coalesce(actual_digest, ''), coalesce(object_version, ''), coalesce(actual_size, 0) FROM artifact_transfers WHERE handle = $1", handle).Scan(&state, &actualDigest, &objectVersion, &actualSize))
	require.Equal(t, "finalized", state)
	require.Equal(t, digestOf(string(body)), actualDigest)
	require.Equal(t, version, objectVersion)
	require.Equal(t, int64(len(body)), actualSize)

	// A wrong digest: the bytes are read back and verified; the caller's
	// upload receipt is not evidence.
	wrong := []byte("a different prompt " + run + "\n")
	code, out = e.call("POST", "/api/v1/artifacts/transfers", tokenA, fmt.Sprintf(`{"commandId":"xfer-%s-w","class":"prompt","expectedDigest":"%s","expectedSize":"%d","mediaType":"text/plain"}`, run, digestOf("something else"), len(wrong)))
	require.Equal(t, 201, code, "%v", out)
	wrongHandle, _ := out["handle"].(string)
	version, status = upload(t, out, wrong)
	require.Equal(t, 200, status)
	code, rejected := e.call("POST", "/api/v1/artifacts/"+wrongHandle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-w","objectVersion":"`+version+`"}`)
	require.Equal(t, 400, code, "%v", rejected)
	require.NoError(t, conn.QueryRow(context.Background(), "SELECT state, coalesce(reason_code, '') FROM artifact_transfers WHERE handle = $1", wrongHandle).Scan(&state, &actualDigest))
	require.Equal(t, "rejected", state)
	require.Equal(t, "DIGEST_MISMATCH", actualDigest)

	// A source archive that escapes its root is rejected at the boundary
	// without being extracted.
	escaping := zipBytes(t, map[string]string{"src/index.tsx": "x", "../evil.sh": "rm -rf /"})
	code, out = e.call("POST", "/api/v1/artifacts/transfers", tokenA, begin("z", "source", "application/zip", escaping))
	require.Equal(t, 201, code, "%v", out)
	zipHandle, _ := out["handle"].(string)
	version, status = upload(t, out, escaping)
	require.Equal(t, 200, status)
	code, rejected = e.call("POST", "/api/v1/artifacts/"+zipHandle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-z","objectVersion":"`+version+`"}`)
	require.Equal(t, 400, code, "%v", rejected)
	require.NoError(t, conn.QueryRow(context.Background(), "SELECT state, coalesce(reason_code, '') FROM artifact_transfers WHERE handle = $1", zipHandle).Scan(&state, &actualDigest))
	require.Equal(t, "rejected", state)
	require.Equal(t, "PATH_ESCAPE", actualDigest)
	// The declared media type does not decide whether the bytes are
	// inspected: the same archive declared as octet-stream is rejected the
	// same way.
	code, out = e.call("POST", "/api/v1/artifacts/transfers", tokenA, begin("o", "source", "application/octet-stream", escaping))
	require.Equal(t, 201, code, "%v", out)
	octetHandle, _ := out["handle"].(string)
	version, status = upload(t, out, escaping)
	require.Equal(t, 200, status)
	code, rejected = e.call("POST", "/api/v1/artifacts/"+octetHandle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-o","objectVersion":"`+version+`"}`)
	require.Equal(t, 400, code, "%v", rejected)
	require.NoError(t, conn.QueryRow(context.Background(), "SELECT state, coalesce(reason_code, '') FROM artifact_transfers WHERE handle = $1", octetHandle).Scan(&state, &actualDigest))
	require.Equal(t, "rejected", state)
	require.Equal(t, "PATH_ESCAPE", actualDigest)

	// A zero-byte transfer: the capability binds the empty body (no
	// length is signed at zero; the backend verifies the signed
	// Content-MD5), refuses any other body and finalizes on zero bytes.
	code, out = e.call("POST", "/api/v1/artifacts/transfers", tokenA, begin("e", "evidence", "text/plain", nil))
	require.Equal(t, 201, code, "%v", out)
	emptyHandle, _ := out["handle"].(string)
	_, status = upload(t, out, []byte("x"))
	require.NotEqual(t, 200, status, "the zero-byte capability refuses a non-empty body")
	version, status = upload(t, out, nil)
	require.Equal(t, 200, status)
	require.NotEmpty(t, version)
	code, fin = e.call("POST", "/api/v1/artifacts/"+emptyHandle+"/finalizations", tokenA, `{"commandId":"fin-`+run+`-e","objectVersion":"`+version+`"}`)
	require.Equal(t, 200, code, "%v", fin)
	require.Equal(t, "finalized", fin["state"])
	require.NoError(t, conn.QueryRow(context.Background(), "SELECT state, coalesce(actual_size, -1) FROM artifact_transfers WHERE handle = $1", emptyHandle).Scan(&state, &actualSize))
	require.Equal(t, "finalized", state)
	require.Zero(t, actualSize)

	// The public contract is validated at the edge: a class outside the
	// reviewed catalog never reaches Control (npm is supported since P10).
	code, out = e.call("POST", "/api/v1/artifacts/transfers", tokenA, `{"commandId":"xfer-`+run+`-unknown","class":"not-reviewed","expectedDigest":"`+digestOf("x")+`","expectedSize":"1","mediaType":"application/octet-stream"}`)
	require.Equal(t, 400, code, "an unreviewed class must be rejected before any upload capability is issued")
}

// TestArtifactAcceptance runs the acceptance path over gRPC against Control
// alone: a transfer bound to an attempt is resolved only by the current
// physical instance, finalized on the bytes, and the result manifest that
// names its handle is accepted as an immutable stage binding the exact
// object version; forged handles, stale epochs, wrong digests and
// conflicting content are refused, and repeats return the original.
func TestArtifactAcceptance(t *testing.T) {
	if os.Getenv("ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT") == "" {
		skipMissing(t, "ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT", "not set; source .local/dev/env.sh after deploy/dev/up.sh")
	}
	e := setupProcesses(t, false, "", "", true)
	run := fmt.Sprint(time.Now().UnixNano())
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	// The scenario plays the API (operations), the Workflow (attempts and
	// launches) and the Job's access sidecar (transfers and the result).
	ops, exec := controlv1.NewOperationServiceClient(e.dialControl(e.controlAddr, principalAPI)), controlv1.NewExecutionServiceClient(e.dialControl(e.controlAddr, principalWorkflow))
	art := controlv1.NewArtifactServiceClient(e.dialControl(e.controlAddr, principalSidecar))
	opID, attemptID := e.fundedOperation(ctx, t, ops, exec, run, "art", 0)
	const image = "sha256:9db7b59979c38555a39def84a31fb98b5296952f9e3afd4f6f11f05b07adfab0"
	launch, err := exec.PrepareLaunch(ctx, &controlv1.PrepareLaunchRequest{
		Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "launch-" + run, ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("launch")},
		AttemptId: attemptID, LaunchKey: "lc-art-" + strings.ToLower(run[len(run)-8:]), Backend: "kind-anvilkit-dev", ImageDigest: image, Deadline: timestamppb.New(time.Now().Add(2 * time.Minute)),
	})
	require.NoError(t, err)
	register := func(cmdID, pod string) *controlv1.PhysicalInstance {
		resp, err := exec.RegisterInstance(ctx, &controlv1.RegisterInstanceRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: cmdID, ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf(cmdID)},
			AttemptId: attemptID, LaunchKey: launch.GetLaunchKey(), Backend: "kind-anvilkit-dev", JobUid: "job-" + run, PodUid: pod, ImageDigest: image,
		})
		require.NoError(t, err)
		return resp.GetInstance()
	}
	owner := register("reg-"+run+"-1", "pod-"+run+"-1")
	duplicate := register("reg-"+run+"-2", "pod-"+run+"-2")
	require.True(t, owner.GetCurrent())
	require.False(t, duplicate.GetCurrent())

	evidence := []byte("observer transcript " + run + "\n")
	scope := &controlv1.Scope{TenantId: "tenant_a", ProjectId: "proj_a", ActorId: "user_a"}
	begun, err := art.BeginTransfer(ctx, &controlv1.BeginTransferRequest{
		Command: &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "xfer-" + run + "-ev", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("ev")},
		Scope:   scope, Class: controlv1.ArtifactClass_ARTIFACT_CLASS_EVIDENCE, ExpectedDigest: digestOf(string(evidence)), ExpectedSize: fmt.Sprint(len(evidence)), MediaType: "text/plain",
		OperationId: &opID, AttemptId: &attemptID, Deadline: timestamppb.New(time.Now().Add(10 * time.Minute)),
	})
	require.NoError(t, err)
	transfer := begun.GetTransfer()
	require.Equal(t, "1", transfer.GetExecutionEpoch())

	// Only the current instance resolves the candidate's handle.
	_, err = art.ResolveHandle(ctx, &controlv1.ResolveHandleRequest{Handle: transfer.GetHandle(), InstanceId: duplicate.GetInstanceId()})
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "a duplicate Pod is not the current owner")
	_, err = art.ResolveHandle(ctx, &controlv1.ResolveHandleRequest{Handle: "hdl_forged", InstanceId: owner.GetInstanceId()})
	require.Equal(t, codes.NotFound, status.Code(err))
	resolved, err := art.ResolveHandle(ctx, &controlv1.ResolveHandleRequest{Handle: transfer.GetHandle(), InstanceId: owner.GetInstanceId()})
	require.NoError(t, err)
	cap := resolved.GetUpload()
	require.NotEmpty(t, cap.GetUrl())
	view := map[string]any{"uploadCapability": cap.GetUrl(), "uploadMethod": cap.GetMethod(), "uploadHeaders": map[string]any{}}
	for k, v := range cap.GetHeaders() {
		view["uploadHeaders"].(map[string]any)[k] = v
	}
	version, code := upload(t, view, evidence)
	require.Equal(t, 200, code)
	require.NotEmpty(t, version)

	_, err = art.GetTransfer(ctx, &controlv1.GetTransferRequest{Handle: transfer.GetHandle(), Scope: &controlv1.Scope{TenantId: "tenant_b", ActorId: "user_b"}})
	require.Equal(t, codes.NotFound, status.Code(err), "another tenant reads nothing")
	finCmd := &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: "fin-" + run + "-ev", ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf("fin")}
	_, err = art.FinalizeTransfer(ctx, &controlv1.FinalizeTransferRequest{Command: finCmd, Handle: transfer.GetHandle(), ObjectVersion: version, InstanceId: duplicate.GetInstanceId()})
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "a duplicate Pod cannot finalize")
	finalized, err := art.FinalizeTransfer(ctx, &controlv1.FinalizeTransferRequest{Command: finCmd, Handle: transfer.GetHandle(), ObjectVersion: version, InstanceId: owner.GetInstanceId()})
	require.NoError(t, err)
	require.Equal(t, controlv1.TransferState_TRANSFER_STATE_FINALIZED, finalized.GetTransfer().GetState())
	got, err := art.GetTransfer(ctx, &controlv1.GetTransferRequest{TransferId: transfer.GetTransferId(), Scope: scope})
	require.NoError(t, err)
	require.Equal(t, version, got.GetTransfer().GetObjectVersion())

	// A certified LocalCheck manifest names the reviewed profile's fixed
	// result (embedded: the reviewed digest and byte size, no object) and
	// then the outputs the case under test adds.
	profile, err := jobschema.ProfileByID("local-check-v1")
	require.NoError(t, err)
	fixed := `{"class":"result","digest":"` + profile.ExpectedResult.ResultDigest + `","sizeBytes":"` + profile.ExpectedResult.ResultSizeBytes + `"}`
	manifestOf := func(outputs string) []byte {
		return []byte(`{"schemaVersion":1,"launchId":"` + launch.GetLaunchId() + `","attemptId":"` + attemptID + `","jobKind":"validator","profileId":"local-check-v1","verdict":"certified","outputs":[` + outputs + `],"completedAt":"2026-09-16T00:00:00Z"}`)
	}
	manifest := func(outputs string) []byte {
		if outputs == "" {
			return manifestOf(fixed)
		}
		return manifestOf(fixed + "," + outputs)
	}
	boundEvidence := `{"class":"evidence","digest":"` + digestOf(string(evidence)) + `","sizeBytes":"` + fmt.Sprint(len(evidence)) + `","handle":"` + transfer.GetHandle() + `"}`
	good := manifest(boundEvidence)
	accept := func(cmdID string, m []byte, instance, epoch string, verdict controlv1.Verdict) (*controlv1.AcceptResultResponse, error) {
		return exec.AcceptResult(ctx, &controlv1.AcceptResultRequest{
			Command:   &controlv1.CommandIdentity{TenantId: "tenant_a", CommandId: cmdID, ActorId: "anvilkit-agent-workflow", RequestDigest: digestOf(cmdID)},
			AttemptId: attemptID, InstanceId: instance, ProfileId: "local-check-v1", Verdict: verdict, ResultDigest: digestOf(string(m)), ResultManifest: m,
			ObserverIdentity: "anvilkit-agent-workflow", ExecutionEpoch: epoch,
		})
	}
	_, err = accept("acc-"+run+"-stale", good, owner.GetInstanceId(), "2", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "a result observed under another epoch is refused")
	_, err = accept("acc-"+run+"-forged", good, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_REPAIRABLE)
	require.Equal(t, codes.InvalidArgument, status.Code(err), "a verdict other than the manifest's is refused")
	forged := manifest(`{"class":"evidence","digest":"` + digestOf(string(evidence)) + `","sizeBytes":"` + fmt.Sprint(len(evidence)) + `","handle":"hdl_forged"}`)
	_, err = accept("acc-"+run+"-handle", forged, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "an output naming no transfer of this scope is refused")
	wrongDigest := manifest(`{"class":"evidence","digest":"` + digestOf("other") + `","sizeBytes":"` + fmt.Sprint(len(evidence)) + `","handle":"` + transfer.GetHandle() + `"}`)
	_, err = accept("acc-"+run+"-digest", wrongDigest, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.InvalidArgument, status.Code(err), "an output whose digest differs from the verified object is refused")
	_, err = accept("acc-"+run+"-dup", good, duplicate.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "a duplicate Pod owns no result")
	emptied := manifest(`{"class":"evidence","digest":"` + digestOf(string(evidence)) + `","sizeBytes":"` + fmt.Sprint(len(evidence)) + `"}`)
	_, err = accept("acc-"+run+"-emptied", emptied, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.InvalidArgument, status.Code(err), "an evidence output without a finalized object is refused")
	arbitrary := manifest(`{"class":"result","digest":"` + digestOf("arbitrary") + `","sizeBytes":"9"}`)
	_, err = accept("acc-"+run+"-arbitrary", arbitrary, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.InvalidArgument, status.Code(err), "an embedded result other than the fixture's reviewed one is refused")
	otherLaunch := []byte(strings.Replace(string(good), launch.GetLaunchId(), "lch_other", 1))
	_, err = accept("acc-"+run+"-launch", otherLaunch, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "a manifest of another launch is not this instance's result")
	_, err = accept("acc-"+run+"-empty", manifestOf(``), owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.InvalidArgument, status.Code(err), "a certified result without the fixture's fixed result is refused")
	_, err = accept("acc-"+run+"-substitute", manifestOf(boundEvidence), owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.InvalidArgument, status.Code(err), "a finalized object of another class does not stand in for the fixed result")
	_, err = accept("acc-"+run+"-size", manifestOf(`{"class":"result","digest":"`+profile.ExpectedResult.ResultDigest+`","sizeBytes":"25"}`), owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.InvalidArgument, status.Code(err), "the reviewed digest with another byte size is not the fixed result")
	_, err = exec.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attemptID, TenantId: "tenant_a"})
	require.Equal(t, codes.NotFound, status.Code(err), "no refusal left a stage behind")

	accepted, err := accept("acc-"+run, good, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.NoError(t, err)
	require.False(t, accepted.GetExisting())
	require.Len(t, accepted.GetStage().GetArtifacts(), 1)
	require.Equal(t, version, accepted.GetStage().GetArtifacts()[0].GetObjectVersion(), "the stage binds the exact verified object version")
	require.Equal(t, "1", accepted.GetStage().GetExecutionEpoch())
	same, err := accept("acc-"+run, good, owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.NoError(t, err)
	require.True(t, same.GetExisting(), "a repeated acceptance returns the original stage")
	require.Equal(t, accepted.GetStage().GetStageId(), same.GetStage().GetStageId())
	_, err = accept("acc-"+run+"-conflict", manifest(``), owner.GetInstanceId(), "1", controlv1.Verdict_VERDICT_CERTIFIED)
	require.Equal(t, codes.Aborted, status.Code(err), "conflicting content never overwrites the accepted result")
	read, err := exec.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attemptID, TenantId: "tenant_a"})
	require.NoError(t, err)
	require.Len(t, read.GetStage().GetArtifacts(), 1)
	require.Equal(t, transfer.GetHandle(), read.GetStage().GetArtifacts()[0].GetHandle())
	_, err = exec.GetAcceptedStage(ctx, &controlv1.GetAcceptedStageRequest{AttemptId: attemptID, TenantId: "tenant_b"})
	require.Equal(t, codes.NotFound, status.Code(err))
	_, err = art.ResolveHandle(ctx, &controlv1.ResolveHandleRequest{Handle: transfer.GetHandle(), InstanceId: owner.GetInstanceId()})
	require.Equal(t, codes.FailedPrecondition, status.Code(err), "a finalized transfer issues no capability")
}
