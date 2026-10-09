package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

// TestMigrationDSN (P0.6): exactly one DSN source; outside development the
// DSN must name sslmode=verify-full in either form; no error echoes the DSN.
func TestMigrationDSN(t *testing.T) {
	const verified = "postgres://anvilkit_knowledge_migrator:pw-in-dsn@db.internal:5432/anvilkit_knowledge?sslmode=verify-full"
	const plain = "postgres://anvilkit_knowledge_migrator:pw-in-dsn@127.0.0.1:25432/anvilkit_knowledge?sslmode=disable"
	file := func(body string) string {
		path := filepath.Join(t.TempDir(), "dsn")
		require.NoError(t, os.WriteFile(path, []byte(body), 0o600))
		return path
	}
	for _, tc := range []struct {
		name, dsn, dsnFile string
		development        bool
		want, err          string
	}{
		{name: "value verify-full", dsn: verified, want: verified},
		{name: "value verify-full with sslrootcert", dsn: verified + "&sslrootcert=/etc/anvilkit/postgres/ca.crt", want: verified + "&sslrootcert=/etc/anvilkit/postgres/ca.crt"},
		{name: "keyword verify-full", dsn: "host=db.internal password='pw-in-dsn' sslmode=verify-full", want: "host=db.internal password='pw-in-dsn' sslmode=verify-full"},
		{name: "file verify-full, trimmed", dsnFile: file(verified + "\n"), want: verified},
		{name: "value disable outside development", dsn: plain, err: `dsn: sslmode must be verify-full outside development (got "disable")`},
		{name: "value require outside development", dsn: "postgres://u:pw-in-dsn@db/anvilkit_mcp?sslmode=require", err: `(got "require")`},
		{name: "value verify-ca outside development", dsn: "postgres://u:pw-in-dsn@db/anvilkit_mcp?sslmode=verify-ca", err: `(got "verify-ca")`},
		{name: "keyword require outside development", dsnFile: file("host=db password=pw-in-dsn sslmode=require\n"), err: `(got "require")`},
		{name: "missing sslmode outside development", dsn: "postgres://u:pw-in-dsn@db/anvilkit_mcp", err: `(got "")`},
		{name: "repeated weaker sslmode", dsn: verified + "&sslmode=disable", err: `(got "disable")`},
		{name: "ssl alias", dsn: verified + "&ssl=true", err: `(got "require")`},
		{name: "unparseable", dsn: "host=db password='pw-in-dsn", err: "not a parseable PostgreSQL DSN"},
		{name: "value disable in development", dsn: plain, development: true, want: plain},
		{name: "file disable in development", dsnFile: file(plain), development: true, want: plain},
		{name: "both sources", dsn: verified, dsnFile: file(verified), err: "mutually exclusive"},
		{name: "no source", err: "is required"},
		{name: "empty file", dsnFile: file("\n"), err: "is empty"},
		{name: "missing file", dsnFile: filepath.Join(t.TempDir(), "absent"), err: "-dsn-file: open "},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := migrationDSN(tc.dsn, tc.dsnFile, tc.development)
			if tc.err != "" {
				require.ErrorContains(t, err, tc.err)
				require.NotContains(t, err.Error(), "pw-in-dsn", "the DSN carries the password and is never echoed")
				return
			}
			require.NoError(t, err)
			require.Equal(t, tc.want, got)
		})
	}
}
