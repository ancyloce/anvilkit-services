//go:build integration

package integration

import (
	"archive/tar"
	"bufio"
	"bytes"
	"context"
	"fmt"
	"io/fs"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.yaml.in/yaml/v3"
)

// TestStudioWorkbench is P20's acceptance run against the Studio host
// (DEVELOPMENT_ONLY placement; ENV-08 gates the real host). It starts the
// API, one Control and one Workflow worker on the disposable dependencies
// of the development foundation (the preview build runs the reviewed
// validator Job in kind; up.sh migrates Control to 00012), seeds one
// preview_build on a fresh lineage of the Pagix source double from the
// fixed Hero source, starts the separate preview origin, and runs the
// Studio Playwright spec e2e/agent-workbench.spec.ts against a Studio dev
// server that forwards to this API. The spec proves concurrent editing
// with conflict preservation and rebase, frame replacement and disposal,
// stale previews, CSS loading in the sandboxed frame, remote releases
// loaded without a host rebuild, type collisions, lossless placeholders and
// exact locks on save and reopen.
//
// ANVILKIT_STUDIO_DIR names the Studio app (default
// ../anvilkit-studio/apps/studio next to this repository); port 3000 must be
// free because the spec's dev server listens there.
func TestStudioWorkbench(t *testing.T) {
	studio := os.Getenv("ANVILKIT_STUDIO_DIR")
	root := repoRoot(t)
	if studio == "" {
		studio = filepath.Join(filepath.Dir(root), "anvilkit-studio", "apps", "studio")
	}
	if _, err := os.Stat(filepath.Join(studio, "e2e", "agent-workbench.spec.ts")); err != nil {
		skipMissing(t, "ANVILKIT_STUDIO_DIR", "the Studio app with e2e/agent-workbench.spec.ts is not at %s", studio)
	}
	if conn, err := net.DialTimeout("tcp", "127.0.0.1:3000", time.Second); err == nil {
		conn.Close()
		t.Fatal("port 3000 is in use: the spec's Studio dev server needs it")
	}
	// The development foundation (not an isolated database): the preview
	// Job runs in kind and reaches the foundation's artifact store.
	scratch := t.TempDir()
	doubles := filepath.Join(scratch, "pagix-double")
	releases := filepath.Join(scratch, "releases")
	require.NoError(t, os.MkdirAll(releases, 0o755))
	// The preview build's reviewed profile (DEVELOPMENT_ONLY: the fixed
	// validator) is enabled for this placement only, as the Workflow's own
	// cluster test does; the checked-in configuration keeps it disabled.
	raw, err := os.ReadFile(filepath.Join(root, "services", "agent", "workflow", "config.yaml"))
	require.NoError(t, err)
	var workflowValues map[string]any
	require.NoError(t, yaml.Unmarshal(raw, &workflowValues))
	workflowValues["kubernetes"].(map[string]any)["enabled_profiles"] = []string{"local-check-v1", "validator-source-dev-v1"}
	raw, err = yaml.Marshal(workflowValues)
	require.NoError(t, err)
	workflowConfig := filepath.Join(scratch, "workflow-preview.yaml")
	require.NoError(t, os.WriteFile(workflowConfig, raw, 0o600))
	// The preview Job's access sidecar reaches Control through the kind
	// gateway, and every transfer capability names the artifact store's
	// cluster-side endpoint (reachable from the host as well).
	gateway, clusterArtifacts := os.Getenv("ANVILKIT_DEV_KIND_GATEWAY"), os.Getenv("ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER")
	if gateway == "" || clusterArtifacts == "" {
		skipMissing(t, "ANVILKIT_DEV_KIND_GATEWAY", "ANVILKIT_DEV_KIND_GATEWAY and ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER not set; source .local/dev/env.sh")
	}
	controlPort := freePort(t)
	t.Setenv("ANVILKIT_INTEGRATION_JOB_CONTROL_LISTEN", "0.0.0.0:"+controlPort)
	e := setupProcessesFull(t, false, workflowConfig, "", false,
		[]string{"ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT=" + clusterArtifacts},
		[]string{"ANVILKIT_WORKFLOW_PAGIX_DOUBLE_DIR=" + doubles, "ANVILKIT_WORKFLOW_SIDECAR_CONTROL_ADDRESS=" + gateway + ":" + controlPort})
	t.Cleanup(func() {
		if t.Failed() {
			t.Logf("workflow log:\n%s\ncontrol log:\n%s", logTail(t, filepath.Join(e.local, "workflow.log"), 80), logTail(t, filepath.Join(e.local, "control.log"), 40))
		}
	})
	run := fmt.Sprint(time.Now().UnixNano())

	// The seed: the fixed Hero source as a deterministic tar, saved on a
	// fresh lineage (revision 1 → 2) and built by the preview Job.
	source := heroSourceTar(t, filepath.Join(root, "jobs", "validator", "fixtures", "component", "hero"))
	code, out := e.call("POST", "/api/v1/artifacts/transfers", tokenA, fmt.Sprintf(`{"commandId":"xfer-src-%s","class":"source","expectedDigest":"%s","expectedSize":"%d","mediaType":"application/x-tar"}`, run, digestOfBytes(source), len(source)))
	require.Equal(t, 201, code, "%v", out)
	version, status := upload(t, out, source)
	require.Equal(t, 200, status)
	handle := out["handle"].(string)
	code, fin := e.call("POST", "/api/v1/artifacts/"+handle+"/finalizations", tokenA, `{"commandId":"fin-src-`+run+`","objectVersion":"`+version+`"}`)
	require.Equal(t, 200, code, "%v", fin)
	lineage := digestOf("studio-lineage-" + run)
	code, op := e.call("POST", "/api/v1/operations", tokenA, fmt.Sprintf(`{"commandId":"seed-%s","kind":"preview_build","subject":{"profileId":"preview-build-v1","subjectDigest":"%s","sourceRevision":"1","sourceHandle":"%s"}}`, run, lineage, handle))
	require.Equal(t, 202, code, "%v", op)
	seed := op["operationId"].(string)
	deadline := time.Now().Add(8 * time.Minute)
	var preview map[string]any
	for time.Now().Before(deadline) {
		code, preview = e.call("GET", "/api/v1/operations/"+seed+"/preview", tokenA, "")
		if code == 200 && (preview["state"] == "ready" || preview["state"] == "failed" || preview["state"] == "conflict") {
			break
		}
		time.Sleep(time.Second)
	}
	require.Equal(t, "ready", preview["state"], "the seed preview: %v", preview)
	require.Equal(t, "2", preview["sourceRevision"])

	// The separate preview origin, embedded only by the Studio origin.
	originPort := freePort(t)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	originCmd := exec.CommandContext(ctx, "node", "dist/preview-origin.js", "--listen", "127.0.0.1:"+originPort, "--parent-origin", "http://localhost:3000")
	originCmd.Dir = filepath.Join(root, "jobs", "validator")
	stdout, err := originCmd.StdoutPipe()
	require.NoError(t, err)
	originCmd.Stderr = os.Stderr
	require.NoError(t, originCmd.Start())
	t.Cleanup(func() { _ = originCmd.Process.Kill(); _ = originCmd.Wait() })
	line, err := bufio.NewReader(stdout).ReadString('\n')
	require.NoError(t, err, "the preview origin did not start (build jobs/validator first)")
	require.Contains(t, line, "previewOrigin")

	// The Studio spec, with a dev server that forwards to this API.
	pw := exec.Command("pnpm", "exec", "playwright", "test", "e2e/agent-workbench.spec.ts", "--project", "chromium", "--workers", "1", "--reporter", "line")
	pw.Dir = studio
	pw.Env = append(os.Environ(),
		"ANVILKIT_AGENT_API_URL="+e.apiURL,
		"ANVILKIT_AGENT_API_TOKEN="+tokenA,
		"ANVILKIT_PREVIEW_ORIGIN=http://127.0.0.1:"+originPort,
		"ANVILKIT_PREVIEW_HOST_PROFILE=host-abi-dev-v1",
		"ANVILKIT_DEV_RELEASES_DIR="+releases,
		"ANVILKIT_E2E_SEED_OPERATION="+seed,
		"ANVILKIT_E2E_PAGIX_DOUBLE="+doubles,
		"ANVILKIT_E2E_LINEAGE="+lineage,
	)
	var output bytes.Buffer
	pw.Stdout, pw.Stderr = &output, &output
	err = pw.Run()
	t.Logf("playwright:\n%s", tailLines(output.String(), 120))
	require.NoError(t, err, "the Studio acceptance spec failed")
	require.NotContains(t, output.String(), "skipped", "every acceptance case ran")
}

// heroSourceTar packs a source tree as the platform writes source
// archives: regular files under sorted relative paths, fixed mode, owner and
// time.
func heroSourceTar(t *testing.T, dir string) []byte {
	t.Helper()
	var paths []string
	require.NoError(t, filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		rel, err := filepath.Rel(dir, p)
		if err != nil {
			return err
		}
		if strings.HasPrefix(rel, "node_modules") {
			return nil
		}
		paths = append(paths, filepath.ToSlash(rel))
		return nil
	}))
	sort.Strings(paths)
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	for _, rel := range paths {
		body, err := os.ReadFile(filepath.Join(dir, rel))
		require.NoError(t, err)
		require.NoError(t, w.WriteHeader(&tar.Header{Name: rel, Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg, ModTime: time.Unix(0, 0), Format: tar.FormatUSTAR}))
		_, err = w.Write(body)
		require.NoError(t, err)
	}
	require.NoError(t, w.Close())
	return buf.Bytes()
}

func tailLines(s string, n int) string {
	lines := strings.Split(s, "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}
