package integration

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

// Untagged helpers: the integration-tagged files and the offline checks without the
// tag (rag_thresholds_test.go, which the go step runs) share them.

func repoRoot(t *testing.T) string {
	wd, err := os.Getwd()
	require.NoError(t, err)
	return filepath.Clean(filepath.Join(wd, "..", ".."))
}

func ragRoot(t *testing.T) string { return filepath.Join(repoRoot(t), "tests", "evals", "rag") }
