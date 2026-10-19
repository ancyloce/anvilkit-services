// Package contracts holds the parent repository's build-closure test of the
// replacement. The consumer/provider agreement tests of the contract fixtures
// live with the contracts (contracts/tests/go, run by contracts/tools/verify.py).
package contracts

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// Build-closure facts of the replacement (delivery.md "Definition of
// complete replacement": no copied old handler, internal import or runtime
// fallback; technology.md: Connect-Go is an alternative, never a primary).
// The service repositories (github.com/ancyloce/anvilkit-agent-{api,control,
// workflow}) hold the replacement implementations at services/agent/{api,
// control,workflow}, so their module paths are legitimate, not legacy
// markers: the legacy modules are recognized by what they imported
// (Connect-Go, the retained contracts and their generated Connect packages
// under internal/contracts) and by the retired paths (the parent-hosted
// module paths of Control and Workflow before their relocation, the
// in-tree generated clients), which the forbidden list covers.
var (
	// expectedModules binds each service directory to the module path its
	// repository publishes; a module under one of these paths with any
	// other name would be a stale or misplaced checkout.
	expectedModules = map[string]string{
		"services/agent/api":      "github.com/ancyloce/anvilkit-agent-api",
		"services/agent/control":  "github.com/ancyloce/anvilkit-agent-control",
		"services/agent/workflow": "github.com/ancyloce/anvilkit-agent-workflow",
	}
	retiredModules = []string{
		"github.com/ancyloce/anvilkit-services/services/anvilkit-agent-control",
		"github.com/ancyloce/anvilkit-services/services/anvilkit-agent-workflow",
		"github.com/ancyloce/anvilkit-services/services/anvilkit-agent-api",
	}
	forbiddenPackages = []string{
		"connectrpc.com/connect",
		"internal/contracts/retained",
		"anvilkit-agent-control/internal/contracts",
		"anvilkit-agent-workflow/internal/contracts",
		"anvilkit-agent-api/internal/contracts",
		"packages/generated-clients",
	}
)

// TestBuildClosureRejectsLegacyModules lists the actual build dependencies
// of every module in go.work and fails if any retired module path, retained
// legacy contract package, the retired in-tree generated-clients path or a
// non-primary RPC framework is part of the closure, and if a service
// directory holds a module under another name than its repository's.
// Legacy code is outside go.work, so this is proof by build dependency, not
// by filename.
func TestBuildClosureRejectsLegacyModules(t *testing.T) {
	root := filepath.Join("..", "..")
	work, err := os.ReadFile(filepath.Join(root, "go.work"))
	require.NoError(t, err)
	modules := regexp.MustCompile(`(?m)^\s*\./(\S+)`).FindAllStringSubmatch(string(work), -1)
	require.NotEmpty(t, modules)
	used := map[string]bool{}
	for _, m := range modules {
		dir := filepath.Join(root, m[1])
		used[m[1]] = true
		name := moduleName(t, dir)
		for _, mod := range retiredModules {
			require.NotEqual(t, mod, name, "%s must not be a retired module path", m[1])
		}
		if want, ok := expectedModules[m[1]]; ok {
			require.Equal(t, want, name, "%s must hold its repository's module", m[1])
		}
		cmd := exec.Command("go", "list", "-deps", "-tags", "integration", "-f", "{{.ImportPath}}", "./...")
		cmd.Dir = dir
		out, err := cmd.CombinedOutput()
		require.NoError(t, err, "%s: %s", m[1], out)
		for _, dep := range strings.Split(strings.TrimSpace(string(out)), "\n") {
			for _, mod := range retiredModules {
				require.False(t, dep == mod || strings.HasPrefix(dep, mod+"/"), "%s depends on retired module %s (%s)", m[1], mod, dep)
			}
			for _, pkg := range forbiddenPackages {
				require.False(t, strings.Contains(dep, pkg), "%s depends on %s (%s)", m[1], pkg, dep)
			}
		}
	}
	for dir := range expectedModules {
		require.True(t, used[dir], "%s must be a go.work module (the replacement's closure)", dir)
	}
}

// moduleName reads the module path declared by the directory's own go.mod
// (go list -m would list every workspace module).
func moduleName(t *testing.T, dir string) string {
	t.Helper()
	cmd := exec.Command("go", "mod", "edit", "-json")
	cmd.Dir = dir
	out, err := cmd.Output()
	require.NoError(t, err)
	var mod struct {
		Module struct{ Path string }
	}
	require.NoError(t, json.Unmarshal(out, &mod))
	return mod.Module.Path
}
