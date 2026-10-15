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
	"errors"
	"fmt"
	"io/fs"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/stdlib"
	"github.com/pressly/goose/v3"
	"github.com/pressly/goose/v3/lock"
)

//go:embed all:sql
var sqlFS embed.FS

// Domains lists the owned databases this Job migrates, in stable order.
var Domains = []string{"knowledge", "mcp"}

// Safety bounds of a migration run (P3.7, plan 0004).
type Options struct {
	// LockTimeout bounds every statement's wait for a table lock: a DDL
	// behind a long transaction fails (SQLSTATE 55P03) instead of queueing
	// every later reader of the table behind its pending exclusive lock.
	LockTimeout time.Duration
	// StatementTimeout bounds one statement (a long backfill or index build).
	StatementTimeout time.Duration
	// Retries is how often a run that hit LockTimeout is retried, RetryDelay
	// apart (growing linearly); goose rolled the failed migration back.
	Retries    int
	RetryDelay time.Duration
	// LockWait bounds the wait for the domain's advisory lock: a second
	// runner waits for the first (one applies, both end at one version)
	// and gives up after LockWait.
	LockWait time.Duration
	// OnRetry observes each retry (logs and tests).
	OnRetry func(attempt int, err error)
}

// DefaultOptions are the Job's bounds.
func DefaultOptions() Options {
	return Options{LockTimeout: 5 * time.Second, StatementTimeout: 15 * time.Minute, Retries: 5, RetryDelay: 2 * time.Second, LockWait: 5 * time.Minute}
}

// lockIDs are the domains' session locks (database-scoped PostgreSQL
// advisory locks; Control's runner uses its own id).
var lockIDs = map[string]int64{"knowledge": 0x616e76696c6b6302, "mcp": 0x616e76696c6b6303}

// Open connects with the migrator role DSN through the pgx stdlib adapter,
// which is what goose drives; the application services keep using pgx/v5
// natively. Every session carries the default bounds' lock_timeout and
// statement_timeout.
func Open(dsn string) (*sql.DB, error) { return OpenWith(dsn, DefaultOptions()) }

// OpenWith is Open with explicit bounds.
func OpenWith(dsn string, o Options) (*sql.DB, error) {
	cfg, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("migrate: parse dsn: %w", err)
	}
	if o.LockTimeout > 0 {
		cfg.RuntimeParams["lock_timeout"] = fmt.Sprintf("%dms", o.LockTimeout.Milliseconds())
	}
	if o.StatementTimeout > 0 {
		cfg.RuntimeParams["statement_timeout"] = fmt.Sprintf("%dms", o.StatementTimeout.Milliseconds())
	}
	return stdlib.OpenDB(*cfg), nil
}

func provider(db *sql.DB, domain string) (*goose.Provider, error) {
	sub, err := domainSource(domain)
	if err != nil {
		return nil, err
	}
	return providerFor(db, domain, sub, DefaultOptions())
}

func domainSource(domain string) (fs.FS, error) {
	sub, err := fs.Sub(sqlFS, "sql/"+domain)
	if err != nil {
		return nil, fmt.Errorf("migrate: unknown domain %q", domain)
	}
	if _, err := fs.Stat(sub, "00001_init.sql"); err != nil {
		return nil, fmt.Errorf("migrate: domain %q has no 00001_init.sql", domain)
	}
	return sub, nil
}

func providerFor(db *sql.DB, domain string, src fs.FS, o Options, extra ...goose.ProviderOption) (*goose.Provider, error) {
	id, ok := lockIDs[domain]
	if !ok {
		return nil, fmt.Errorf("migrate: unknown domain %q", domain)
	}
	wait := o.LockWait
	if wait <= 0 {
		wait = DefaultOptions().LockWait
	}
	// goose polls pg_try_advisory_lock every period seconds, failureThreshold times.
	const period = 1
	locker, err := lock.NewPostgresSessionLocker(lock.WithLockID(id), lock.WithLockTimeout(period, uint64(max(1, wait/time.Second))))
	if err != nil {
		return nil, err
	}
	opts := append([]goose.ProviderOption{goose.WithVerbose(false), goose.WithSessionLocker(locker)}, extra...)
	return goose.NewProvider(goose.DialectPostgres, db, src, opts...)
}

// LockTimedOut reports whether err is a statement's lock_timeout (SQLSTATE 55P03).
func LockTimedOut(err error) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == "55P03"
}

// Up applies every pending migration of the domain and returns the resulting version.
func Up(ctx context.Context, db *sql.DB, domain string) (int64, error) {
	return UpWith(ctx, db, domain, DefaultOptions())
}

// UpWith is Up with explicit bounds: a run whose statement hit lock_timeout
// is retried up to o.Retries times (goose rolled that migration back); any
// other error ends the run.
func UpWith(ctx context.Context, db *sql.DB, domain string, o Options) (int64, error) {
	sub, err := domainSource(domain)
	if err != nil {
		return 0, err
	}
	return upFS(ctx, db, domain, sub, o)
}

func upFS(ctx context.Context, db *sql.DB, domain string, src fs.FS, o Options, extra ...goose.ProviderOption) (int64, error) {
	p, err := providerFor(db, domain, src, o, extra...)
	if err != nil {
		return 0, err
	}
	for attempt := 1; ; attempt++ {
		_, err := p.Up(ctx)
		if err == nil {
			break
		}
		if !LockTimedOut(err) || attempt > o.Retries {
			return 0, fmt.Errorf("migrate: up %s: %w", domain, err)
		}
		if o.OnRetry != nil {
			o.OnRetry(attempt, err)
		}
		select {
		case <-ctx.Done():
			return 0, ctx.Err()
		case <-time.After(o.RetryDelay * time.Duration(attempt)):
		}
	}
	return p.GetDBVersion(ctx)
}

// DownTo rolls the domain back to the given version; version 0 is an empty
// schema. It destroys data: the Job calls it only with -destructive.
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
	return providerFor(db, domain, sub, DefaultOptions(), goose.WithTableName(FixtureTable))
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
