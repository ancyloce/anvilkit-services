// Package migrate applies the owned domain schemas that have no service
// repository yet with goose/v3 (A07): it is the migration-version authority
// for anvilkit_knowledge and anvilkit_mcp until Knowledge and MCP own their
// migrations. anvilkit_control is migrated by Control's own migration Job
// (the anvilkit-agent-control repository, internal/migrate and
// cmd/anvilkit-migration). Runtime services never run DDL.
package migrate

import (
	"context"
	"database/sql"
	"embed"
	"fmt"
	"io/fs"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
)

//go:embed all:sql
var sqlFS embed.FS

// Domains lists the owned databases this Job migrates, in stable order.
var Domains = []string{"knowledge", "mcp"}

// Open connects with the migrator role DSN through the pgx stdlib adapter,
// which is what goose drives; the application services keep using pgx/v5
// natively.
func Open(dsn string) (*sql.DB, error) {
	cfg, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("migrate: parse dsn: %w", err)
	}
	return stdlib.OpenDB(*cfg), nil
}

func provider(db *sql.DB, domain string) (*goose.Provider, error) {
	sub, err := fs.Sub(sqlFS, "sql/"+domain)
	if err != nil {
		return nil, fmt.Errorf("migrate: unknown domain %q", domain)
	}
	if _, err := fs.Stat(sub, "00001_init.sql"); err != nil {
		return nil, fmt.Errorf("migrate: domain %q has no 00001_init.sql", domain)
	}
	return goose.NewProvider(goose.DialectPostgres, db, sub, goose.WithVerbose(false))
}

// Up applies every pending migration of the domain and returns the resulting version.
func Up(ctx context.Context, db *sql.DB, domain string) (int64, error) {
	p, err := provider(db, domain)
	if err != nil {
		return 0, err
	}
	if _, err := p.Up(ctx); err != nil {
		return 0, fmt.Errorf("migrate: up %s: %w", domain, err)
	}
	return p.GetDBVersion(ctx)
}

// DownTo rolls the domain back to the given version; version 0 is an empty schema.
func DownTo(ctx context.Context, db *sql.DB, domain string, version int64) (int64, error) {
	p, err := provider(db, domain)
	if err != nil {
		return 0, err
	}
	if _, err := p.DownTo(ctx, version); err != nil {
		return 0, fmt.Errorf("migrate: down %s: %w", domain, err)
	}
	return p.GetDBVersion(ctx)
}

// FixtureTable is the version table of a domain's DEVELOPMENT_ONLY fixture
// set (sql/<domain>-development), kept apart from the domain's own.
const FixtureTable = "goose_db_version_development"

// HasFixtures reports whether the domain has a development fixture set.
func HasFixtures(domain string) bool {
	_, err := fs.Stat(sqlFS, "sql/"+domain+"-development")
	return err == nil
}

func fixtureProvider(db *sql.DB, domain string) (*goose.Provider, error) {
	sub, err := fs.Sub(sqlFS, "sql/"+domain+"-development")
	if err != nil {
		return nil, fmt.Errorf("migrate: domain %q has no fixture set", domain)
	}
	return goose.NewProvider(goose.DialectPostgres, db, sub, goose.WithVerbose(false), goose.WithTableName(FixtureTable))
}

// UpFixtures applies the domain's DEVELOPMENT_ONLY fixture set (P2.5) after
// its own migrations; a domain without one is left as it is. Only the
// Job's -development mode calls it.
func UpFixtures(ctx context.Context, db *sql.DB, domain string) (int64, error) {
	if !HasFixtures(domain) {
		return 0, nil
	}
	p, err := fixtureProvider(db, domain)
	if err != nil {
		return 0, err
	}
	if _, err := p.Up(ctx); err != nil {
		return 0, fmt.Errorf("migrate: fixtures %s: %w", domain, err)
	}
	return p.GetDBVersion(ctx)
}

// DownFixtures rolls the fixture set back completely (before any rollback
// of the domain's own migrations it was applied over).
func DownFixtures(ctx context.Context, db *sql.DB, domain string) error {
	if !HasFixtures(domain) {
		return nil
	}
	p, err := fixtureProvider(db, domain)
	if err != nil {
		return err
	}
	if _, err := p.DownTo(ctx, 0); err != nil {
		return fmt.Errorf("migrate: fixtures down %s: %w", domain, err)
	}
	return nil
}

// Version reports the applied version without changing anything.
func Version(ctx context.Context, db *sql.DB, domain string) (int64, error) {
	p, err := provider(db, domain)
	if err != nil {
		return 0, err
	}
	return p.GetDBVersion(ctx)
}
