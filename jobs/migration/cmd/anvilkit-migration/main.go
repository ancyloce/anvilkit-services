// anvilkit-migration is the migration Job class (architecture.md naming: job
// kind "migration") of the domains without a service repository yet
// (knowledge, mcp; Control migrates through its own repository's Job). It
// applies one owned domain's schema with the domain's migrator role and
// exits. It never reads legacy tables or runtime config.
//
//	anvilkit-migration -domain knowledge (-dsn "$ANVILKIT_MIGRATION_DSN" | -dsn-file "$ANVILKIT_MIGRATION_DSN_FILE") [-development] [-to N] [-status]
//
// The DSN comes from exactly one source: the value, or a mounted secret
// file (the OpenBao CSI volume, P0.6) read once. Outside development it
// must name sslmode=verify-full; -development (or
// ANVILKIT_MIGRATION_DEVELOPMENT=true) admits the plaintext DSN of the
// development foundation (DEVELOPMENT_ONLY).
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"github.com/ancyloce/anvilkit-services/jobs/migration/internal/migrate"
)

func main() {
	var (
		domain      = flag.String("domain", "", "owned domain: knowledge or mcp")
		dsn         = flag.String("dsn", os.Getenv("ANVILKIT_MIGRATION_DSN"), "migrator-role DSN (default $ANVILKIT_MIGRATION_DSN)")
		dsnFile     = flag.String("dsn-file", os.Getenv("ANVILKIT_MIGRATION_DSN_FILE"), "mounted secret file holding the migrator-role DSN (default $ANVILKIT_MIGRATION_DSN_FILE)")
		development = flag.Bool("development", os.Getenv("ANVILKIT_MIGRATION_DEVELOPMENT") == "true", "DEVELOPMENT_ONLY: admit a DSN without sslmode=verify-full (default $ANVILKIT_MIGRATION_DEVELOPMENT == true)")
		to          = flag.Int64("to", -1, "roll back to this version instead of applying pending migrations")
		status      = flag.Bool("status", false, "print the applied version and exit")
	)
	flag.Parse()
	if *domain == "" {
		fmt.Fprintln(os.Stderr, "anvilkit-migration: -domain is required")
		os.Exit(2)
	}
	resolved, err := migrationDSN(*dsn, *dsnFile, *development)
	if err != nil {
		fmt.Fprintln(os.Stderr, "anvilkit-migration:", err)
		os.Exit(2)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()

	db, err := migrate.Open(resolved)
	if err != nil {
		fail(err)
	}
	defer db.Close()

	var version int64
	switch {
	case *status:
		version, err = migrate.Version(ctx, db, *domain)
	case *to >= 0:
		version, err = migrate.DownTo(ctx, db, *domain, *to)
	default:
		version, err = migrate.Up(ctx, db, *domain)
	}
	if err != nil {
		fail(err)
	}
	fmt.Printf("domain=%s version=%d\n", *domain, version)
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "anvilkit-migration:", err)
	os.Exit(1)
}
