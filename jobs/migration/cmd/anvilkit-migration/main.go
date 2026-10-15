// anvilkit-migration is the migration Job class (architecture.md naming: job
// kind "migration") of the domains without a service repository yet
// (knowledge, mcp; Control migrates through its own repository's Job). It
// applies one owned domain's schema with the domain's migrator role and
// exits. It never reads legacy tables or runtime config.
//
//	anvilkit-migration -domain knowledge (-dsn "$ANVILKIT_MIGRATION_DSN" | -dsn-file "$ANVILKIT_MIGRATION_DSN_FILE") [-development] [-to N -destructive] [-status]
//
// A rollback (-to N) drops what later migrations created and is refused
// without -destructive (P3.7). Every session runs under lock_timeout and
// statement_timeout, serialized with any other runner of the domain on its
// advisory lock (internal/migrate Options).
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
		destructive = flag.Bool("destructive", false, "permit -to: a rollback drops what later migrations created")
	)
	flag.Parse()
	if err := rollbackAllowed(*to, *destructive); err != nil {
		fmt.Fprintln(os.Stderr, "anvilkit-migration:", err)
		os.Exit(2)
	}
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
		// The fixture set sits on top of the domain's migrations: it goes first.
		if *development {
			if err = migrate.DownFixtures(ctx, db, *domain); err != nil {
				fail(err)
			}
		}
		version, err = migrate.DownTo(ctx, db, *domain, *to)
	default:
		version, err = migrate.Up(ctx, db, *domain)
		// DEVELOPMENT_ONLY (P2.5): the domain's fixture set (sql/<domain>-development)
		// re-admits the fixture kinds the production schema refuses.
		if err == nil && *development {
			var fixtures int64
			if fixtures, err = migrate.UpFixtures(ctx, db, *domain); err == nil && migrate.HasFixtures(*domain) {
				fmt.Printf("domain=%s fixtures=%d\n", *domain, fixtures)
			}
		}
	}
	if err != nil {
		fail(err)
	}
	fmt.Printf("domain=%s version=%d\n", *domain, version)
}

// rollbackAllowed refuses a rollback that was not asked for as destructive.
func rollbackAllowed(to int64, destructive bool) error {
	if to >= 0 && !destructive {
		return fmt.Errorf("-to %d rolls the schema back and drops data: refused without -destructive", to)
	}
	return nil
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "anvilkit-migration:", err)
	os.Exit(1)
}
