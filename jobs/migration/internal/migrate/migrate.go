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

// Version reports the applied version without changing anything.
func Version(ctx context.Context, db *sql.DB, domain string) (int64, error) {
	p, err := provider(db, domain)
	if err != nil {
		return 0, err
	}
	return p.GetDBVersion(ctx)
}
