//go:build integration

package integration

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"google.golang.org/grpc"
	"google.golang.org/grpc/health"
	"google.golang.org/grpc/health/grpc_health_v1"
)

// TestKnowledgePublicAPI (P2.6 AC1/AC2, registry IT-KNOWLEDGE-PUBLIC): the
// knowledge journey through the public Agent API alone, with nothing
// written to the bucket by the test: a document is uploaded through
// Knowledge's single-use capability (beginSourceUpload, then the presigned
// PUT with exactly the signed headers), registered, observed to `indexed`,
// frozen in a snapshot, found by a cited search; a fact citing the source
// is proposed, confirmed and recalled. The capability is refused a second
// time and for another tenant.
//
// It runs the lane's Knowledge (parser Job on the foundation's kind cluster,
// Inference, its own Qdrant) behind a real API process authenticated by the
// DEVELOPMENT_ONLY fixture principals, as the API workload over mTLS.
//
//	. .local/dev/env.sh && go test -tags integration -count=1 -run TestKnowledgePublicAPI ./tests/integration/
func TestKnowledgePublicAPI(t *testing.T) {
	laneEnv(t, "ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT", "ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT", "ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE",
		"ANVILKIT_KNOWLEDGE_KUBECONFIG", "ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY", "ANVILKIT_DEV_KNOWLEDGE_MIGRATOR_DSN", "ANVILKIT_DEV_INFERENCE_URL",
		"ANVILKIT_KNOWLEDGE_QDRANT_URL", "ANVILKIT_KNOWLEDGE_QDRANT_API_KEY")
	infURL := inferenceURL(t)
	qdrantURL, qdrantKey := ownQdrant(t)
	t.Setenv("ANVILKIT_KNOWLEDGE_QDRANT_URL", qdrantURL)
	t.Setenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY", qdrantKey)
	l := setupLaneWith(t, laneOptions{
		knowledgeReplacements: [][2]string{{`  profile: ""`, "  profile: parser-docling-dev-v1"}, {"  node_pool: parsing", "  node_pool: components"}, inferenceTLS(t)},
		knowledgeEnv: []string{
			"ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_ENDPOINT"),
			"ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_STAGE_ENDPOINT"),
			"ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE=" + os.Getenv("ANVILKIT_KNOWLEDGE_OBJECTS_CREDENTIALS_FILE"),
			"ANVILKIT_KNOWLEDGE_KUBECONFIG=" + os.Getenv("ANVILKIT_KNOWLEDGE_KUBECONFIG"),
			"ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY=" + os.Getenv("ANVILKIT_KNOWLEDGE_IMAGE_REGISTRY"),
			"ANVILKIT_KNOWLEDGE_INFERENCE_URL=" + infURL,
			"ANVILKIT_KNOWLEDGE_QDRANT_URL=" + os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_URL"),
			"ANVILKIT_KNOWLEDGE_QDRANT_API_KEY=" + os.Getenv("ANVILKIT_KNOWLEDGE_QDRANT_API_KEY"),
		},
	})
	run := strconv.FormatInt(time.Now().Unix(), 36)
	tenantA, tenantB := "pub-"+run+"-a", "pub-"+run+"-b"
	api := startPublicAPI(t, l, map[string]string{
		"token-alice-" + run: tenantA + "/alice",
		"token-bob-" + run:   tenantB + "/bob",
	})
	alice, bob := "token-alice-"+run, "token-bob-"+run
	seq := 0
	cmd := func() string { seq++; return fmt.Sprintf("p26-%s-%d", run, seq) }

	doc := []byte("# Launch brief\n\nThe launch hero uses the saffron accent on a slate background.\n\nCalls to action stay in the first viewport.\n")
	var sourceID, snapshotID string
	t.Run("a document is uploaded through the capability, registered and indexed; the capability is single-use", func(t *testing.T) {
		code, up := api.call("POST", "/api/v1/knowledge/sources/uploads", alice, map[string]any{
			"commandId": cmd(), "contentDigest": sha(doc), "sizeBytes": strconv.Itoa(len(doc)), "mediaType": "text/markdown"})
		require.Equal(t, http.StatusCreated, code, "%v", up)
		uploadID := up["uploadId"].(string)
		headers := map[string]string{}
		for k, v := range up["uploadHeaders"].(map[string]any) {
			headers[k] = v.(string)
		}
		require.Equal(t, http.StatusOK, presignedPut(t, up["uploadUrl"].(string), headers, doc), "the store admits exactly the declared bytes")
		register := map[string]any{"commandId": cmd(), "kind": "document", "locator": "upload:" + uploadID, "contentDigest": sha(doc),
			"mediaType": "text/markdown", "sizeBytes": strconv.Itoa(len(doc)), "access": []map[string]any{{"principalType": "tenant", "principalId": tenantA}}}
		code, src := api.call("POST", "/api/v1/knowledge/sources", alice, register)
		require.Equal(t, http.StatusAccepted, code, "%v", src)
		sourceID = src["sourceId"].(string)
		register["commandId"] = cmd()
		code, again := api.call("POST", "/api/v1/knowledge/sources", alice, register)
		require.GreaterOrEqual(t, code, 400, "the capability is consumed: %v", again)
		code, foreign := api.call("POST", "/api/v1/knowledge/sources", bob, map[string]any{"commandId": cmd(), "kind": "document", "locator": "upload:" + uploadID,
			"contentDigest": sha(doc), "mediaType": "text/markdown", "sizeBytes": strconv.Itoa(len(doc))})
		require.GreaterOrEqual(t, code, 400, "another tenant's capability is unknown: %v", foreign)
		l.eventually(20*time.Minute, "source "+sourceID+" indexed", func() bool {
			code, s := api.call("GET", "/api/v1/knowledge/sources/"+sourceID, alice, nil)
			require.Equal(t, http.StatusOK, code, "%v", s)
			require.NotEqual(t, "failed", s["ingest"], "%v", s)
			return s["ingest"] == "indexed"
		})
	})

	t.Run("a snapshot freezes the revision and search cites the chunk", func(t *testing.T) {
		require.NotEmpty(t, sourceID)
		code, snap := api.call("POST", "/api/v1/knowledge/snapshots", alice, map[string]any{"commandId": cmd(), "sourceIds": []string{sourceID}})
		require.Equal(t, http.StatusCreated, code, "%v", snap)
		snapshotID = snap["snapshotId"].(string)
		require.Equal(t, []any{sourceID + "@1"}, snap["sourceRevisions"])
		var items []any
		l.eventually(5*time.Minute, "a cited answer", func() bool {
			code, res := api.call("POST", "/api/v1/knowledge/searches", alice, map[string]any{"snapshotId": snapshotID,
				"query": "which accent does the launch hero use?", "retrievalProfileId": "hybrid-bge-m3-dev-v1"})
			require.Equal(t, http.StatusOK, code, "%v", res)
			items, _ = res["items"].([]any)
			return len(items) > 0
		})
		first := items[0].(map[string]any)
		require.Equal(t, sourceID, first["citation"].(map[string]any)["sourceId"])
		require.Contains(t, first["text"], "saffron")
		code, other := api.call("POST", "/api/v1/knowledge/searches", bob, map[string]any{"snapshotId": snapshotID, "query": "saffron", "retrievalProfileId": "hybrid-bge-m3-dev-v1"})
		require.GreaterOrEqual(t, code, 400, "another tenant never reads the snapshot: %v", other)
	})

	t.Run("a fact citing the source is proposed, confirmed and recalled", func(t *testing.T) {
		require.NotEmpty(t, sourceID)
		code, fact := api.call("POST", "/api/v1/memories", alice, map[string]any{"commandId": cmd(), "subjectType": "actor", "subjectId": "alice",
			"content": "alice wants the saffron accent on launch heroes", "sourceRefs": []string{sourceID + "@1"}})
		require.Equal(t, http.StatusCreated, code, "%v", fact)
		factID := fact["factId"].(string)
		code, decided := api.call("POST", "/api/v1/memories/"+factID+"/decisions", alice, map[string]any{"commandId": cmd(),
			"expectedRevision": fact["revision"], "decision": "confirm"})
		require.Equal(t, http.StatusOK, code, "%v", decided)
		require.Equal(t, "confirmed", decided["state"])
		l.eventually(5*time.Minute, "the fact recalled", func() bool {
			code, res := api.call("POST", "/api/v1/memories/recalls", alice, map[string]any{"query": "which accent does alice want?", "retrievalProfileId": "hybrid-bge-m3-dev-v1"})
			require.Equal(t, http.StatusOK, code, "%v", res)
			for _, f := range res["facts"].([]any) {
				if f.(map[string]any)["factId"] == factID {
					return true
				}
			}
			return false
		})
	})
}

type publicAPI struct {
	t    *testing.T
	base string
}

// startPublicAPI runs the API binary in front of the lane's Knowledge: mTLS
// as the API workload, fixture principals (token -> tenant/actor), a
// placeholder Control (the journey does not reach it).
func startPublicAPI(t *testing.T, l *lane, principals map[string]string) *publicAPI {
	t.Helper()
	root := repoRoot(t)
	bin := filepath.Join(l.dir, "anvilkit-agent-api")
	build := exec.Command("go", "build", "-o", bin, "./cmd/anvilkit-agent-api")
	build.Dir = filepath.Join(root, "services", "agent", "api")
	raw, err := build.CombinedOutput()
	require.NoError(t, err, string(raw))
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	gs := grpc.NewServer()
	hs := health.NewServer()
	grpc_health_v1.RegisterHealthServer(gs, hs)
	go func() { _ = gs.Serve(lis) }()
	t.Cleanup(gs.Stop)
	table := map[string]map[string]any{}
	for token, who := range principals {
		tenant, actor, _ := strings.Cut(who, "/")
		table[token] = map[string]any{"tenantId": tenant, "projectId": "proj_pub", "actorId": actor, "roles": []string{}}
	}
	pfile := filepath.Join(l.dir, "api-principals.json")
	b, err := json.Marshal(table)
	require.NoError(t, err)
	require.NoError(t, os.WriteFile(pfile, b, 0o600))
	addr := "127.0.0.1:" + freePort(t)
	cfg := fmt.Sprintf(`development:
  enabled: true
http:
  listen: %s
control:
  address: %s
  identity:
    mode: development
knowledge:
  timeout: 30s
  identity:
    mode: mtls
    mtls:
      server_name: anvilkit-agent-knowledge
auth:
  mode: fixture
  principals_file: %s
`, addr, lis.Addr(), pfile)
	cfile := filepath.Join(l.dir, "api.yaml")
	require.NoError(t, os.WriteFile(cfile, []byte(cfg), 0o600))
	logf, err := os.Create(filepath.Join(l.dir, "api.log"))
	require.NoError(t, err)
	proc := exec.Command(bin)
	// The caller's ANVILKIT_API_ variables (a sourced .local/dev/env.sh names
	// the development Control) would outrank the configuration written here.
	for _, e := range os.Environ() {
		if !strings.HasPrefix(e, "ANVILKIT_API_") {
			proc.Env = append(proc.Env, e)
		}
	}
	proc.Env = append(append(proc.Env, "ANVILKIT_API_CONFIG="+cfile, "ANVILKIT_API_KNOWLEDGE_ADDRESS="+l.knowledgeAddr),
		identityEnv(t, "ANVILKIT_API_", principalAPI)...)
	proc.Stdout, proc.Stderr = logf, logf
	require.NoError(t, proc.Start())
	t.Cleanup(func() { _ = proc.Process.Kill(); _, _ = proc.Process.Wait(); logf.Close() })
	a := &publicAPI{t: t, base: "http://" + addr}
	require.Eventually(t, func() bool {
		resp, err := http.Get(a.base + "/readyz")
		if err != nil {
			return false
		}
		resp.Body.Close()
		return resp.StatusCode == http.StatusNoContent
	}, 60*time.Second, 200*time.Millisecond, "the API is ready (see %s)", filepath.Join(l.dir, "api.log"))
	return a
}

func (a *publicAPI) call(method, path, token string, body any) (int, map[string]any) {
	a.t.Helper()
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		require.NoError(a.t, err)
		rd = bytes.NewReader(b)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, a.base+path, rd)
	require.NoError(a.t, err)
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	require.NoError(a.t, err)
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	return resp.StatusCode, out
}

// presignedPut performs the capability's PUT with exactly its signed headers.
func presignedPut(t *testing.T, url string, headers map[string]string, body []byte) int {
	t.Helper()
	req, err := http.NewRequest(http.MethodPut, url, bytes.NewReader(body))
	require.NoError(t, err)
	for k, v := range headers {
		if strings.EqualFold(k, "content-length") {
			require.Equal(t, strconv.Itoa(len(body)), v)
			continue
		}
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	require.NoError(t, err)
	resp.Body.Close()
	return resp.StatusCode
}
