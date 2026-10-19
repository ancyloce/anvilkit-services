//go:build integration

package integration

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"go.yaml.in/yaml/v3"
)

// TestRelease is P21's acceptance run on the development foundation
// (DEVELOPMENT_ONLY: the Pagix review, publication and activation ports
// are the Workflow's filesystem double until ENV-07; the Studio host is
// not qualified, ENV-08). It starts the API, one Control and one Workflow
// worker (the certification runs the reviewed validator Job in kind; up.sh
// migrates Control to 00014), saves fixed Hero sources on fresh lineages
// through preview builds and releases their saved revisions:
//
//	A  the exact subject approved after the wait rolled over (continue as
//	   new); the browser publication's and the activation's answers are
//	   lost and resolved by their original identities: activated, one
//	   publication per effect, the catalog names exactly the release, the
//	   Studio pins it and reopens it exactly;
//	B  an approval of another digest: rejected, nothing published;
//	C  a browser receipt naming another destination: partially published,
//	   never activated, npm never resent;
//	D  the catalog moves before the conditional activation: conflict, the
//	   catalog is not overwritten;
//	E  the published version again: npm refused (VERSION_EXISTS), the
//	   browser target never sent;
//	F  a lineage without an allocated component identity: the release is
//	   refused (IDENTITY_UNALLOCATED, P0.8);
//	G  the same lineage allocated another Puck type: the certification
//	   refuses the source's own declaration (IDENTITY_MISMATCH), nothing
//	   published. The lineages seeded by previews get the identity a
//	   Generation's registration would have bound, written directly.
func TestRelease(t *testing.T) {
	root := repoRoot(t)
	gateway, clusterArtifacts := os.Getenv("ANVILKIT_DEV_KIND_GATEWAY"), os.Getenv("ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER")
	if gateway == "" || clusterArtifacts == "" {
		skipMissing(t, "ANVILKIT_DEV_KIND_GATEWAY", "ANVILKIT_DEV_KIND_GATEWAY and ANVILKIT_DEV_ARTIFACTS_ENDPOINT_CLUSTER not set; source .local/dev/env.sh")
	}
	scratch := t.TempDir()
	doubles := filepath.Join(scratch, "pagix-double")
	releases := filepath.Join(scratch, "releases")
	require.NoError(t, os.MkdirAll(releases, 0o755))
	raw, err := os.ReadFile(filepath.Join(root, "services", "agent", "workflow", "config.yaml"))
	require.NoError(t, err)
	var workflowValues map[string]any
	require.NoError(t, yaml.Unmarshal(raw, &workflowValues))
	workflowValues["kubernetes"].(map[string]any)["enabled_profiles"] = []string{"local-check-v1", "validator-source-dev-v1"}
	release := workflowValues["lifecycle"].(map[string]any)["release"].(map[string]any)
	release["approval_poll"], release["polls_per_run"], release["reconcile_pause"], release["reconcile_rounds"] = "1s", 10, "1s", 5
	raw, err = yaml.Marshal(workflowValues)
	require.NoError(t, err)
	workflowConfig := filepath.Join(scratch, "workflow-release.yaml")
	require.NoError(t, os.WriteFile(workflowConfig, raw, 0o600))
	controlPort := freePort(t)
	t.Setenv("ANVILKIT_INTEGRATION_JOB_CONTROL_LISTEN", "0.0.0.0:"+controlPort)
	e := setupProcessesFull(t, false, workflowConfig, "", false,
		[]string{"ANVILKIT_CONTROL_ARTIFACTS_S3_ENDPOINT=" + clusterArtifacts},
		[]string{"ANVILKIT_WORKFLOW_PAGIX_DOUBLE_DIR=" + doubles, "ANVILKIT_WORKFLOW_SIDECAR_CONTROL_ADDRESS=" + gateway + ":" + controlPort,
			"ANVILKIT_WORKFLOW_RELEASE_ORIGIN_DIR=" + releases})
	t.Cleanup(func() {
		if t.Failed() {
			t.Logf("workflow log:\n%s\ncontrol log:\n%s", logTail(t, filepath.Join(e.local, "workflow.log"), 120), logTail(t, filepath.Join(e.local, "control.log"), 40))
		}
	})
	run := fmt.Sprint(time.Now().UnixNano())

	// saved seeds a fresh lineage with the fixed Hero at a version and
	// answers the preview operation holding its saved revision 2.
	saved := func(name, version string) (lineage, op string) {
		src := filepath.Join(scratch, "hero-"+name)
		fixture := filepath.Join(root, "jobs", "validator", "fixtures", "component", "hero")
		require.NoError(t, filepath.WalkDir(fixture, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				return err
			}
			rel, _ := filepath.Rel(fixture, p)
			if strings.HasPrefix(rel, "node_modules") {
				return filepath.SkipDir
			}
			if d.IsDir() {
				return os.MkdirAll(filepath.Join(src, rel), 0o755)
			}
			body, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			if rel == "package.json" {
				body = []byte(strings.Replace(string(body), `"version": "1.0.0"`, `"version": "`+version+`"`, 1))
			}
			return os.WriteFile(filepath.Join(src, rel), body, 0o644)
		}))
		source := heroSourceTar(t, src)
		code, out := e.call("POST", "/api/v1/artifacts/transfers", tokenA, fmt.Sprintf(`{"commandId":"xfer-rel-%s-%s","class":"source","expectedDigest":"%s","expectedSize":"%d","mediaType":"application/x-tar"}`, name, run, digestOfBytes(source), len(source)))
		require.Equal(t, 201, code, "%v", out)
		objectVersion, status := upload(t, out, source)
		require.Equal(t, 200, status)
		handle := out["handle"].(string)
		code, fin := e.call("POST", "/api/v1/artifacts/"+handle+"/finalizations", tokenA, `{"commandId":"fin-rel-`+name+`-`+run+`","objectVersion":"`+objectVersion+`"}`)
		require.Equal(t, 200, code, "%v", fin)
		lineage = digestOf("release-lineage-" + name + "-" + run)
		code, created := e.call("POST", "/api/v1/operations", tokenA, fmt.Sprintf(`{"commandId":"prv-%s-%s","kind":"preview_build","subject":{"profileId":"preview-build-v1","subjectDigest":"%s","sourceRevision":"1","sourceHandle":"%s"}}`, name, run, lineage, handle))
		require.Equal(t, 202, code, "%v", created)
		op = created["operationId"].(string)
		var preview map[string]any
		for deadline := time.Now().Add(8 * time.Minute); time.Now().Before(deadline); time.Sleep(time.Second) {
			code, preview = e.call("GET", "/api/v1/operations/"+op+"/preview", tokenA, "")
			if code == 200 && (preview["state"] == "ready" || preview["state"] == "failed" || preview["state"] == "conflict") {
				break
			}
		}
		require.Equal(t, "ready", preview["state"], "seed %s: %v", name, preview)
		require.Equal(t, "2", preview["sourceRevision"])
		return lineage, op
	}
	// allocate records the component identity a Generation's candidate
	// registration binds to its lineage (P0.8); these lineages are seeded by
	// a preview without a Generation, so the scenario writes the record
	// Control would have written.
	allocate := func(lineage, puckType string) {
		_, err := e.db().Exec(context.Background(), `INSERT INTO lineage_identities (tenant_id, lineage, component_id, puck_type, package_name, operation_id, brief_id, recorded_at)
			VALUES ('tenant_a', $1, 'cmp_hero_fixed', $2, '@anvilkit/hero-fixed', 'op_fixture', 'brf_fixture', now())`, lineage, puckType)
		require.NoError(t, err)
	}
	request := func(name, lineage, sourceOp, version string) string {
		code, out := e.call("POST", "/api/v1/operations", tokenA, fmt.Sprintf(`{"commandId":"rel-%s-%s","kind":"release","subject":{"profileId":"release-v1","subjectDigest":"%s","sourceRevision":"2","sourceOperationId":"%s","packageVersion":"%s"}}`, name, run, lineage, sourceOp, version))
		require.Equal(t, 202, code, "%v", out)
		return out["operationId"].(string)
	}
	waitRelease := func(op string, states ...string) map[string]any {
		var rel map[string]any
		for deadline := time.Now().Add(8 * time.Minute); time.Now().Before(deadline); time.Sleep(time.Second) {
			code, body := e.call("GET", "/api/v1/operations/"+op+"/release", tokenA, "")
			if code == 200 {
				rel = body
				for _, s := range states {
					if rel["state"] == s {
						return rel
					}
				}
				if rel["state"] == "failed" || rel["state"] == "rejected" {
					break
				}
			}
		}
		t.Fatalf("release %s never reached %v: %v", op, states, rel)
		return nil
	}
	waitSettled := func(op string) map[string]any {
		var view map[string]any
		for deadline := time.Now().Add(3 * time.Minute); time.Now().Before(deadline); time.Sleep(time.Second) {
			_, view = e.call("GET", "/api/v1/operations/"+op, tokenA, "")
			if l := view["lifecycle"]; l == "succeeded" || l == "failed" || l == "canceled" || l == "reconciling" {
				return view
			}
		}
		t.Fatalf("operation %s never settled: %v", op, view)
		return nil
	}
	decide := func(releaseID, state, subjectDigest string) {
		body, err := json.Marshal(map[string]any{"state": state, "subjectDigest": subjectDigest, "approverId": "maintainer_a", "decidedAt": time.Now().UTC()})
		require.NoError(t, err)
		path := filepath.Join(doubles, "decisions", releaseID+".json")
		require.NoError(t, os.WriteFile(path+".tmp", body, 0o600))
		require.NoError(t, os.Rename(path+".tmp", path))
	}
	fault := func(key, value string) {
		require.NoError(t, os.WriteFile(filepath.Join(doubles, "faults", key), []byte(value), 0o600))
	}
	publications := func() int {
		entries, err := os.ReadDir(filepath.Join(doubles, "publications"))
		require.NoError(t, err)
		return len(entries)
	}
	catalog := func() map[string]any {
		var doc map[string]any
		raw, err := os.ReadFile(filepath.Join(releases, "catalog.json"))
		if os.IsNotExist(err) {
			return map[string]any{"revision": "0", "releases": []any{}}
		}
		require.NoError(t, err)
		require.NoError(t, json.Unmarshal(raw, &doc))
		return doc
	}
	target := func(rel map[string]any, name string) map[string]any { return rel[name].(map[string]any) }

	lineageA, previewA := saved("a", "1.0.0")
	allocate(lineageA, "Hero")

	// A: activated after the rolled-over wait and two lost answers.
	opA := request("a", lineageA, previewA, "1.0.0")
	relA := waitRelease(opA, "awaiting_approval")
	subjectA := relA["subject"].(map[string]any)
	digestA := subjectA["subjectDigest"].(string)
	require.Equal(t, "2", subjectA["sourceRevision"], "the revision the source authority named for the bytes")
	require.Equal(t, "1.0.0", subjectA["version"])
	require.Equal(t, map[string]any{"npmRegistry": "https://registry.anvilkit.invalid/", "browserOrigin": "https://components.anvilkit.invalid"}, subjectA["destinations"])
	require.Equal(t, "pending", relA["approval"].(map[string]any)["state"])
	require.Equal(t, digestA, relA["approval"].(map[string]any)["subjectDigest"])
	require.Equal(t, 0, publications(), "nothing is published before the approval")
	// The wait rolls over (10 polls of 1 s per run) before the decision.
	time.Sleep(15 * time.Second)
	fault("publish:2", "unknown")
	fault("activate:1", "unknown")
	decide(relA["releaseId"].(string), "approved", digestA)
	relA = waitRelease(opA, "activated")
	require.Equal(t, "succeeded", waitSettled(opA)["lifecycle"])
	require.Equal(t, 2, publications(), "one publication per target effect; the lost answer was queried, never resent")
	for _, name := range []string{"npm", "browser", "activation"} {
		require.Equal(t, "succeeded", target(relA, name)["state"], name)
	}
	require.Equal(t, "https://components.anvilkit.invalid", target(relA, "browser")["destination"])
	lockA := relA["lock"].(map[string]any)
	require.Equal(t, relA["releaseId"], lockA["releaseId"])
	manifest, err := os.ReadFile(filepath.Join(releases, lockA["releaseId"].(string), "browser-manifest.json"))
	require.NoError(t, err)
	require.Equal(t, digestOfBytes(manifest), lockA["browserManifestDigest"], "the origin serves exactly the locked manifest")
	cat := catalog()
	require.Len(t, cat["releases"], 1)
	entry := cat["releases"].([]any)[0].(map[string]any)
	delete(entry, "label")
	require.Equal(t, lockA, entry, "the catalog names exactly the activated release")
	catalogAfterA := cat["revision"]

	// B: a decision for another digest authorizes nothing.
	opB := request("b", lineageA, previewA, "1.0.0")
	relB := waitRelease(opB, "awaiting_approval")
	decide(relB["releaseId"].(string), "approved", digestOf("another subject"))
	relB = waitRelease(opB, "rejected")
	require.Equal(t, "APPROVAL_SUBJECT_MISMATCH", relB["failureCode"])
	require.Equal(t, "pending", target(relB, "npm")["state"])
	require.Equal(t, 2, publications())

	// C: a browser receipt naming another destination.
	lineageC, previewC := saved("c", "1.0.1")
	allocate(lineageC, "Hero")
	opC := request("c", lineageC, previewC, "1.0.1")
	relC := waitRelease(opC, "awaiting_approval")
	fault("publish:2", "wrong-destination")
	decide(relC["releaseId"].(string), "approved", relC["subject"].(map[string]any)["subjectDigest"].(string))
	relC = waitRelease(opC, "partially_published")
	require.Equal(t, "succeeded", target(relC, "npm")["state"])
	require.Equal(t, "failed", target(relC, "browser")["state"])
	require.Equal(t, "DESTINATION_MISMATCH", target(relC, "browser")["failureCode"])
	require.Equal(t, "pending", target(relC, "activation")["state"], "npm-only success is never activated")
	require.Nil(t, relC["lock"])
	require.Equal(t, "PARTIALLY_PUBLISHED", waitSettled(opC)["failureCode"])
	require.Equal(t, 4, publications(), "npm once, browser once; nothing resent")
	require.Equal(t, catalogAfterA, catalog()["revision"])

	// D: the catalog moves between the read and the conditional activation.
	lineageD, previewD := saved("d", "1.0.2")
	allocate(lineageD, "Hero")
	opD := request("d", lineageD, previewD, "1.0.2")
	relD := waitRelease(opD, "awaiting_approval")
	fault("activate:1", "conflict")
	decide(relD["releaseId"].(string), "approved", relD["subject"].(map[string]any)["subjectDigest"].(string))
	relD = waitRelease(opD, "failed")
	require.Equal(t, "ACTIVATION_CONFLICT", relD["failureCode"])
	require.Equal(t, "succeeded", target(relD, "npm")["state"])
	require.Equal(t, "succeeded", target(relD, "browser")["state"])
	cat = catalog()
	require.Len(t, cat["releases"], 1, "the conflicting activation wrote nothing")
	entry = cat["releases"].([]any)[0].(map[string]any)
	require.Equal(t, lockA["releaseId"], entry["releaseId"])

	// E: the published version is never written again.
	opE := request("e", lineageA, previewA, "1.0.0")
	relE := waitRelease(opE, "awaiting_approval")
	decide(relE["releaseId"].(string), "approved", relE["subject"].(map[string]any)["subjectDigest"].(string))
	relE = waitRelease(opE, "failed")
	require.Equal(t, "VERSION_EXISTS", relE["failureCode"])
	require.Equal(t, "pending", target(relE, "browser")["state"], "the browser target is not sent after a refused npm target")

	// F (P0.8): a lineage without an allocated component identity is never
	// released; Control refuses the request itself.
	lineageF, previewF := saved("f", "1.0.3")
	code, refused := e.call("POST", "/api/v1/operations", tokenA, fmt.Sprintf(`{"commandId":"rel-f-%s","kind":"release","subject":{"profileId":"release-v1","subjectDigest":"%s","sourceRevision":"2","sourceOperationId":"%s","packageVersion":"1.0.3"}}`, run, lineageF, previewF))
	require.Equal(t, 400, code, "%v", refused)
	require.Contains(t, fmt.Sprint(refused), "IDENTITY_UNALLOCATED")

	// G (P0.8): what is certified and released is the identity allocated to
	// the lineage, never the source's own declaration: the fixed Hero on a
	// lineage allocated another Puck type fails certification and publishes
	// nothing.
	allocate(lineageF, "Banner")
	before := publications()
	opG := request("g", lineageF, previewF, "1.0.3")
	relG := waitRelease(opG, "failed")
	require.Equal(t, "IDENTITY_MISMATCH", relG["failureCode"])
	require.Equal(t, before, publications())

	// The Studio pins and reopens A exactly. Without the Studio app this leg
	// skips (P3.3): the evaluator records the case NOT_RUN, never a pass.
	t.Run("studio pins and reopens the released lock", func(t *testing.T) {
		studio := os.Getenv("ANVILKIT_STUDIO_DIR")
		if studio == "" {
			studio = filepath.Join(filepath.Dir(root), "anvilkit-studio", "apps", "studio")
		}
		if _, err := os.Stat(filepath.Join(studio, "e2e", "release.spec.ts")); err != nil {
			skipMissing(t, "ANVILKIT_STUDIO_DIR", "the Studio app with e2e/release.spec.ts is not at %s", studio)
		}
		if conn, err := net.DialTimeout("tcp", "127.0.0.1:3000", time.Second); err == nil {
			conn.Close()
			t.Fatal("port 3000 is in use: the spec's Studio dev server needs it")
		}
		lockJSON, err := json.Marshal(lockA)
		require.NoError(t, err)
		pw := exec.Command("pnpm", "exec", "playwright", "test", "e2e/release.spec.ts", "--project", "chromium", "--workers", "1", "--reporter", "line")
		pw.Dir = studio
		pw.Env = append(os.Environ(),
			"ANVILKIT_AGENT_API_URL="+e.apiURL,
			"ANVILKIT_AGENT_API_TOKEN="+tokenA,
			"ANVILKIT_DEV_RELEASES_DIR="+releases,
			"ANVILKIT_E2E_RELEASE_OPERATION="+opA,
			"ANVILKIT_E2E_RELEASE_LOCK="+string(lockJSON),
		)
		out, err := pw.CombinedOutput()
		t.Logf("playwright:\n%s", tailLines(string(out), 80))
		require.NoError(t, err, "the Studio release spec failed")
		require.NotContains(t, string(out), "skipped", "every release case ran")
	})
}
