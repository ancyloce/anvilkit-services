//go:build integration

package integration

import (
	"fmt"
	"testing"
)

// skipMissing skips t because an input of its environment is missing. The
// first line of the skip reads "MISSING_INPUT <input>: <reason>", the
// machine-readable form tools/run-evaluation.py records as the case's
// missing input (P3.3): a skip is NOT_RUN, never a pass. <input> is a
// stable token: an environment variable name, "root" (a root caller),
// "build:<dir>" (an unbuilt package output), "tool:<name>", "image:<name>",
// "file:<path>" or "ENV-nn" (a deployment input).
func skipMissing(t testing.TB, input, format string, args ...any) {
	t.Helper()
	t.Skipf("MISSING_INPUT %s: %s", input, fmt.Sprintf(format, args...))
}
