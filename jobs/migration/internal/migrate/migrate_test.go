package migrate_test

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	"github.com/testcontainers/testcontainers-go"
	"github.com/testcontainers/testcontainers-go/modules/postgres"

	"github.com/ancyloce/anvilkit-services/jobs/migration/internal/migrate"
)

// pgImage is the business PostgreSQL line of the baseline (technology.md:
// PostgreSQL 17.11). The exact patch is whatever the pinned image resolves to.
const pgImage = "postgres:17-alpine"

// latest is the migration version each domain reaches from an empty database.
var latest = map[string]int64{"knowledge": 13, "mcp": 9}

// TestInitialSchemas proves, on a real PostgreSQL, that every domain this Job
// owns installs from an empty database with 00001_init.sql (plus its forward
// migrations), that the runtime role receives DML but no DDL, and that the
// migrations are reversible. Control's schema and its forward-migration
// proof live with Control (anvilkit-agent-control, internal/migrate).
func TestInitialSchemas(t *testing.T) {
	if os.Getenv("ANVILKIT_SKIP_DOCKER_TESTS") != "" {
		t.Skip("ANVILKIT_SKIP_DOCKER_TESTS set")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()

	pg, err := postgres.Run(ctx, pgImage,
		postgres.WithUsername("postgres"), postgres.WithPassword("postgres"), postgres.WithDatabase("postgres"),
		postgres.BasicWaitStrategies())
	require.NoError(t, err)
	testcontainers.CleanupContainer(t, pg)

	adminDSN, err := pg.ConnectionString(ctx, "sslmode=disable")
	require.NoError(t, err)
	admin, err := sql.Open("pgx", adminDSN)
	require.NoError(t, err)
	defer admin.Close()

	for _, domain := range migrate.Domains {
		domain := domain
		t.Run(domain, func(t *testing.T) {
			app := "anvilkit_" + domain + "_app"
			migrator := "anvilkit_" + domain + "_migrator"
			dbName := "anvilkit_" + domain
			// The relay identity of both domains and the Knowledge forwarder
			// identity exist before 00002 grants to them (deploy/dev/postgres-init.sh).
			stmts := []string{
				fmt.Sprintf("CREATE ROLE %s LOGIN PASSWORD 'app'", app),
				fmt.Sprintf("CREATE ROLE %s LOGIN PASSWORD 'migrator'", migrator),
				fmt.Sprintf("CREATE ROLE anvilkit_%s_relay LOGIN PASSWORD 'relay'", domain),
				fmt.Sprintf("CREATE DATABASE %s OWNER %s", dbName, migrator),
			}
			if domain == "knowledge" {
				// P17: the PostgresStore vendor migration identity that 00005 lets
				// create its own schema.
				stmts = append(stmts, "CREATE ROLE anvilkit_knowledge_forwarder LOGIN PASSWORD 'forwarder'",
					"CREATE ROLE anvilkit_knowledge_store_migrator LOGIN PASSWORD 'store'",
					// P2.7: the cleanup identity that 00011 grants to.
					"CREATE ROLE anvilkit_knowledge_cleanup LOGIN PASSWORD 'cleanup'")
			}
			for _, stmt := range stmts {
				_, err := admin.ExecContext(ctx, stmt)
				require.NoError(t, err, stmt)
			}
			host, err := pg.Host(ctx)
			require.NoError(t, err)
			port, err := pg.MappedPort(ctx, "5432/tcp")
			require.NoError(t, err)
			dsn := func(role, pw string) string {
				return fmt.Sprintf("postgres://%s:%s@%s:%s/%s?sslmode=disable", role, pw, host, port.Port(), dbName)
			}

			db, err := migrate.Open(dsn(migrator, "migrator"))
			require.NoError(t, err)
			defer db.Close()

			version, err := migrate.Up(ctx, db, domain)
			require.NoError(t, err)
			require.Equal(t, latest[domain], version, "00001_init.sql plus the domain's forward migrations")

			var tables int
			require.NoError(t, db.QueryRowContext(ctx, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'goose_db_version'").Scan(&tables))
			require.Greater(t, tables, 5, "owned tables exist")

			// The runtime role can write rows but cannot change the schema.
			appDB, err := migrate.Open(dsn(app, "app"))
			require.NoError(t, err)
			defer appDB.Close()
			_, err = appDB.ExecContext(ctx, "CREATE TABLE forbidden (x int)")
			require.Error(t, err, "app role must not hold DDL")
			require.NoError(t, appDB.QueryRowContext(ctx, "SELECT count(*) FROM goose_db_version").Scan(new(int)),
				"app role may read (grants applied)")
			_, err = appDB.ExecContext(ctx, "DELETE FROM goose_db_version")
			require.Error(t, err, "app role has no DELETE on migration bookkeeping")
			// P3.7 AC5 (knowledge 00013, mcp 00008): no write of the bookkeeping at all.
			for _, stmt := range []string{"INSERT INTO goose_db_version (version_id, is_applied) VALUES (999, true)", "UPDATE goose_db_version SET is_applied = false"} {
				_, err = appDB.ExecContext(ctx, stmt)
				require.ErrorContains(t, err, "permission denied", stmt)
			}

			// 00002: the outbox physical schema of the pinned watermill-sql
			// adapter is writable by the app role in a domain transaction; the
			// relay identity reads requests and writes its inbox only; the
			// Knowledge forwarder identity reads messages and owns offsets only.
			_, err = appDB.ExecContext(ctx, `INSERT INTO outbox (uuid, payload, metadata, transaction_id) VALUES ('0192a1b2-3c4d-7e5f-8a9b-0c1d2e3f4a5b', '{}', '{}', pg_current_xact_id())`)
			require.NoError(t, err, "app role publishes into the outbox")
			relayDB, err := migrate.Open(dsn("anvilkit_"+domain+"_relay", "relay"))
			require.NoError(t, err)
			defer relayDB.Close()
			require.NoError(t, relayDB.QueryRowContext(ctx, "SELECT count(*) FROM background_requests").Scan(new(int)), "relay reads durable requests")
			_, err = relayDB.ExecContext(ctx, "INSERT INTO inbox (event_id, consumer, outcome, aggregate_revision) VALUES ('0192a1b2-3c4d-7e5f-8a9b-0c1d2e3f4a5c', 'relay-test', 'enqueued', 1)")
			require.NoError(t, err, "relay commits inbox entries")
			_, err = relayDB.ExecContext(ctx, "UPDATE background_requests SET state = 'dead'")
			require.Error(t, err, "relay cannot write domain facts")
			_, err = relayDB.ExecContext(ctx, "SELECT count(*) FROM outbox")
			require.Error(t, err, "relay cannot read the outbox")
			if domain == "knowledge" {
				fwdDB, err := migrate.Open(dsn("anvilkit_knowledge_forwarder", "forwarder"))
				require.NoError(t, err)
				defer fwdDB.Close()
				require.NoError(t, fwdDB.QueryRowContext(ctx, "SELECT count(*) FROM outbox").Scan(new(int)), "forwarder reads messages")
				_, err = fwdDB.ExecContext(ctx, "INSERT INTO outbox_offsets (consumer_group, offset_acked, last_processed_transaction_id) VALUES ('forwarder', 0, '0') ON CONFLICT DO NOTHING")
				require.NoError(t, err, "forwarder owns its offsets")
				_, err = fwdDB.ExecContext(ctx, "SELECT count(*) FROM background_requests")
				require.Error(t, err, "forwarder has no domain-table access")
				_, err = fwdDB.ExecContext(ctx, "DELETE FROM outbox")
				require.Error(t, err, "forwarder cannot delete messages")
				_, err = fwdDB.ExecContext(ctx, "CREATE TABLE forbidden (x int)")
				require.Error(t, err, "forwarder has no DDL")

				// 00006: the app role reads the forwarder's acknowledged position
				// for its outbox-lag observation (the query Knowledge runs), and
				// nothing more: the offsets stay the forwarder's to write.
				require.NoError(t, appDB.QueryRowContext(ctx, `SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0)::float8 FROM outbox
					WHERE (transaction_id, "offset") > (SELECT COALESCE(max(last_processed_transaction_id), '0'::xid8), COALESCE(max(offset_acked), 0) FROM outbox_offsets WHERE consumer_group = $1)`,
					"forwarder").Scan(new(float64)), "app role observes the unforwarded backlog")
				_, err = appDB.ExecContext(ctx, "UPDATE outbox_offsets SET offset_acked = offset_acked")
				require.Error(t, err, "app role cannot move the forwarder's offsets")
				_, err = appDB.ExecContext(ctx, "INSERT INTO outbox_offsets (consumer_group, offset_acked, last_processed_transaction_id) VALUES ('app', 0, '0')")
				require.Error(t, err, "app role cannot add offsets")

				// 00003: the app role registers sources with their verified
				// object, ACL history and command ledger, records parse
				// launches and chunks, but never deletes ACL history; a chunk
				// identity binds the parser and chunker revisions.
				for _, stmt := range []string{
					"INSERT INTO sources (source_id, tenant_id, kind, locator, command_id, request_digest) VALUES ('src-m', 't1', 'document', 'upload:a.md', 'c1', 'sha256:" + strings.Repeat("a", 64) + "')",
					"INSERT INTO source_revisions (source_id, revision, content_digest, media_type, size_bytes, object_key) VALUES ('src-m', 1, 'sha256:" + strings.Repeat("b", 64) + "', 'text/markdown', 3, 'knowledge/sources/t1/" + strings.Repeat("b", 64) + "')",
					"INSERT INTO source_acl_revisions (source_id, acl_revision, actor_id, command_id) VALUES ('src-m', 1, 'u1', 'c1')",
					"INSERT INTO source_commands (tenant_id, command_id, command_kind, request_digest, source_id) VALUES ('t1', 'c1', 'register', 'sha256:" + strings.Repeat("a", 64) + "', 'src-m')",
					"INSERT INTO ingest_requests (request_id, source_id, source_revision, state, parser_profile, parser_profile_revision, chunker_profile, chunker_revision, task_id) VALUES ('ing-m', 'src-m', 1, 'pending', 'parser-docling-dev-v1', 1, 'docling-hierarchical', 1, 'ingest-src-m')",
					"INSERT INTO chunks (chunk_id, source_id, source_revision, parser_profile, parser_profile_revision, chunker_profile, chunker_revision, ordinal, locator, content_digest, content_ref, ingest_request_id) VALUES ('ch-1', 'src-m', 1, 'parser-docling-dev-v1', 1, 'docling-hierarchical', 1, 0, '{}', 'sha256:" + strings.Repeat("c", 64) + "', 'r#0', 'ing-m')",
					"INSERT INTO chunks (chunk_id, source_id, source_revision, parser_profile, parser_profile_revision, chunker_profile, chunker_revision, ordinal, locator, content_digest, content_ref, ingest_request_id) VALUES ('ch-2', 'src-m', 1, 'parser-docling-dev-v1', 1, 'docling-hierarchical', 2, 0, '{}', 'sha256:" + strings.Repeat("c", 64) + "', 'r#0', 'ing-m')",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.NoError(t, err, stmt)
				}
				_, err = appDB.ExecContext(ctx, "INSERT INTO chunks (chunk_id, source_id, source_revision, parser_profile, parser_profile_revision, chunker_profile, chunker_revision, ordinal, locator, content_digest, content_ref, ingest_request_id) VALUES ('ch-3', 'src-m', 1, 'parser-docling-dev-v1', 1, 'docling-hierarchical', 1, 0, '{}', 'sha256:"+strings.Repeat("c", 64)+"', 'r#0', 'ing-m')")
				require.Error(t, err, "one chunk per source revision, parser/chunker revision and ordinal")
				_, err = appDB.ExecContext(ctx, "DELETE FROM source_acl_revisions")
				require.Error(t, err, "ACL history is append-only for the app role")
				// 00004: one index entry per generation and source revision; a
				// materialized entry carries its manifest digest and every
				// point; one accepted generation at a time.
				for _, stmt := range []string{
					"INSERT INTO index_generations (generation, model_id, model_revision, chunker_profile, collection_name, state, embedding_profile, dimensions, sparse_profile, chunker_revision, accepted_at) VALUES (1, 'bge-m3', 'r1', 'docling-hierarchical', 'anvilkit-knowledge-1', 'accepted', 'bge-m3-v1', 1024, 'bge-m3-lexical-v1', 1, now())",
					"INSERT INTO index_entries (generation, source_id, source_revision, ingest_request_id, task_id, state, chunk_count) VALUES (1, 'src-m', 1, 'ing-m', 'index-src-m-r1-g1', 'pending', 2)",
					"UPDATE index_entries SET state = 'materialized', point_count = 2, manifest_digest = 'sha256:" + strings.Repeat("d", 64) + "' WHERE task_id = 'index-src-m-r1-g1'",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.NoError(t, err, stmt)
				}
				_, err = appDB.ExecContext(ctx, "UPDATE index_entries SET state = 'accepted', point_count = 1 WHERE task_id = 'index-src-m-r1-g1'")
				require.Error(t, err, "an accepted entry covers every chunk")
				_, err = appDB.ExecContext(ctx, "INSERT INTO index_generations (generation, model_id, model_revision, chunker_profile, collection_name, state, embedding_profile, dimensions, sparse_profile, chunker_revision, accepted_at) VALUES (2, 'bge-m3', 'r1', 'docling-hierarchical', 'anvilkit-knowledge-2', 'accepted', 'bge-m3-v1', 1024, 'bge-m3-lexical-v1', 1, now())")
				require.Error(t, err, "one accepted generation")
				_, err = appDB.ExecContext(ctx, "DELETE FROM index_entries")
				require.Error(t, err, "the app role never deletes index entries")
				// 00005: facts record their origin; decisions are append-only
				// and idempotent per (tenant, command); a model or worker
				// authority only proposes; one live confirmed fact per
				// content, subject and scope; a deleted fact has no content;
				// a materialized projection carries its digest; memory-project
				// requests are authorized by their fact.
				digest := "sha256:" + strings.Repeat("e", 64)
				fact := func(id, cmd string) string {
					return "INSERT INTO memory_facts (fact_id, tenant_id, subject_type, subject_id, scope_id, content, content_digest, state, revision, proposer, confirmer, command_id, request_digest, origin) VALUES ('" + id + "', 't1', 'actor', 'u1', 'p1', 'likes tea', '" + digest + "', 'confirmed', 2, 'model-a', 'u1', '" + cmd + "', '" + digest + "', 'model')"
				}
				for _, stmt := range []string{
					fact("mem-1", "mc1"),
					"INSERT INTO memory_decisions (decision_id, fact_id, from_revision, to_revision, decision, decider, command_id, request_digest, tenant_id, authority) VALUES ('d1', 'mem-1', 0, 1, 'propose', 'model-a', 'mc1', '" + digest + "', 't1', 'model')",
					"INSERT INTO memory_decisions (decision_id, fact_id, from_revision, to_revision, decision, decider, command_id, request_digest, tenant_id, authority) VALUES ('d2', 'mem-1', 1, 2, 'confirm', 'u1', 'mc2', '" + digest + "', 't1', 'user')",
					"INSERT INTO memory_projections (fact_id, target, fact_revision, action, task_id, state) VALUES ('mem-1', 0, 2, 'apply', 'memproj-mem-1-t0', 'pending')",
					"INSERT INTO background_requests (task_id, generation, tenant_id, task_kind, input_digest, input, state, result_profile, authorization_ref, correlation_id) VALUES ('memproj-mem-1-t0', 1, 't1', 'memory-project', '" + digest + "', '{}', 'pending', 'memory-project-v1', 'memory:mem-1', 'mc2')",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.NoError(t, err, stmt)
				}
				for stmt, why := range map[string]string{
					fact("mem-2", "mc3"): "one live confirmed fact per content, subject and scope",
					"INSERT INTO memory_decisions (decision_id, fact_id, from_revision, to_revision, decision, decider, command_id, request_digest, tenant_id, authority) VALUES ('d3', 'mem-1', 2, 3, 'confirm', 'model-a', 'mc4', '" + digest + "', 't1', 'model')": "a model authority only proposes",
					"INSERT INTO memory_decisions (decision_id, fact_id, from_revision, to_revision, decision, decider, command_id, request_digest, tenant_id, authority) VALUES ('d4', 'mem-1', 2, 3, 'revoke', 'u1', 'mc2', '" + digest + "', 't1', 'user')":        "one decision per (tenant, command)",
					"INSERT INTO memory_decisions (decision_id, fact_id, from_revision, to_revision, decision, decider, command_id, request_digest, tenant_id, authority) VALUES ('d5', 'mem-1', 2, 3, 'expire', 'rule', 'mc5', '" + digest + "', 't1', 'policy')":    "a reviewed rule names its policy revision",
					"UPDATE memory_decisions SET decider = 'u2'":                                                      "decisions are append-only for the app role",
					"DELETE FROM memory_decisions":                                                                    "decisions are append-only for the app role",
					"UPDATE memory_facts SET deleted = true, deleted_at = now() WHERE fact_id = 'mem-1'":              "a deleted fact keeps no content",
					"UPDATE memory_projections SET state = 'materialized' WHERE fact_id = 'mem-1'":                    "a materialized projection carries its digest",
					"UPDATE background_requests SET authorization_ref = 'grant:x' WHERE task_id = 'memproj-mem-1-t0'": "memory-project requests are authorized by a source or a fact",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.Error(t, err, why)
				}
				_, err = appDB.ExecContext(ctx, "UPDATE memory_facts SET deleted = true, deleted_at = now(), content = '', revision = 3 WHERE fact_id = 'mem-1'")
				require.NoError(t, err, "deletion erases the content")
				// 00007: removals taken before the migration are backfilled as
				// pending rows (their records are written by Knowledge);
				// the app role records and marks removal rows but never
				// deletes one; a row binds its revisions, a revocation its
				// confirmer, a recorded row its inventory key, and its time
				// is held at millisecond precision.
				version, err = migrate.DownTo(ctx, db, domain, 6)
				require.NoError(t, err)
				require.Equal(t, int64(6), version)
				for _, stmt := range []string{
					"INSERT INTO memory_decisions (decision_id, fact_id, from_revision, to_revision, decision, decider, command_id, request_digest, tenant_id, authority) VALUES ('d6', 'mem-1', 2, 3, 'delete', 'u1', 'mc6', '" + digest + "', 't1', 'user')",
					"INSERT INTO memory_facts (fact_id, tenant_id, subject_type, subject_id, scope_id, content, content_digest, state, revision, proposer, confirmer, command_id, request_digest, origin) VALUES ('mem-3', 't1', 'actor', 'u1', 'p1', 'likes coffee', '" + digest + "', 'revoked', 3, 'model-a', 'u1', 'mc7', '" + digest + "', 'model')",
					"INSERT INTO memory_decisions (decision_id, fact_id, from_revision, to_revision, decision, decider, reason_code, command_id, request_digest, tenant_id, authority) VALUES ('d7', 'mem-3', 2, 3, 'revoke', 'u2', 'OUTDATED', 'mc8', '" + digest + "', 't1', 'user')",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.NoError(t, err, stmt)
				}
				version, err = migrate.Up(ctx, db, domain)
				require.NoError(t, err)
				require.Equal(t, latest[domain], version)
				var backfilled []string
				rows, err := appDB.QueryContext(ctx, "SELECT command_id || ':' || decision || ':' || state || ':' || confirmer || ':' || reason_code || ':' || (inventory_key IS NULL)::text FROM memory_removals ORDER BY command_id")
				require.NoError(t, err)
				for rows.Next() {
					var x string
					require.NoError(t, rows.Scan(&x))
					backfilled = append(backfilled, x)
				}
				require.NoError(t, rows.Err())
				require.Equal(t, []string{"mc6:delete:pending:::true", "mc8:revoke:pending:u1:OUTDATED:true"}, backfilled)
				removal := func(cmd, decision, confirmer, from, to, at, key, state string) string {
					return "INSERT INTO memory_removals (tenant_id, command_id, fact_id, decision, decider, confirmer, request_digest, from_revision, to_revision, recorded_at, inventory_key, state) VALUES ('t1', '" + cmd + "', 'mem-1', '" + decision + "', 'u1', '" + confirmer + "', '" + digest + "', " + from + ", " + to + ", '" + at + "', " + key + ", '" + state + "')"
				}
				// One removal scope per database, created once; the app role only reads it.
				var scopes int
				require.NoError(t, appDB.QueryRowContext(ctx, "SELECT count(*) FROM memory_removal_scope WHERE scope_id IS NOT NULL").Scan(&scopes))
				require.Equal(t, 1, scopes, "one removal scope")
				for stmt, why := range map[string]string{
					"INSERT INTO memory_removal_scope DEFAULT VALUES":              "the app role never adds a scope",
					"UPDATE memory_removal_scope SET scope_id = gen_random_uuid()": "the app role never changes the scope",
					"DELETE FROM memory_removal_scope":                             "the app role never deletes the scope",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.Error(t, err, why)
				}
				_, err = db.ExecContext(ctx, "INSERT INTO memory_removal_scope DEFAULT VALUES")
				require.Error(t, err, "a database has one removal scope")
				_, err = appDB.ExecContext(ctx, removal("mc9", "delete", "", "3", "4", "2026-10-02T15:01:12.123Z", "'removals/k9.json'", "pending"))
				require.NoError(t, err, "the app role records a removal")
				_, err = appDB.ExecContext(ctx, "UPDATE memory_removals SET state = 'recorded' WHERE command_id = 'mc9'")
				require.NoError(t, err, "the app role marks a removal recorded")
				for stmt, why := range map[string]string{
					"DELETE FROM memory_removals": "removal rows are never deleted by the app role",
					removal("mc6", "delete", "", "3", "4", "2026-10-02T15:01:12.123Z", "NULL", "pending"):                 "one removal per (tenant, command)",
					removal("mc10", "delete", "", "3", "5", "2026-10-02T15:01:12.123Z", "NULL", "pending"):                "a removal is one revision step",
					removal("mc11", "revoke", "", "3", "4", "2026-10-02T15:01:12.123Z", "NULL", "pending"):                "a revocation names its confirmer",
					removal("mc12", "delete", "", "3", "4", "2026-10-02T15:01:12.123Z", "NULL", "recorded"):               "a recorded removal has its inventory key",
					removal("mc13", "delete", "", "3", "4", "2026-10-02T15:01:12.1234Z", "NULL", "pending"):               "a removal's time is held at millisecond precision",
					removal("mc14", "delete", "", "3", "4", "2026-10-02T15:01:12.123Z", "'removals/k9.json'", "restored"): "one row per inventory key",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.Error(t, err, why)
				}
				// The Store's vendor identity may create its own schema; the
				// app role may not.
				storeDB, err := migrate.Open(dsn("anvilkit_knowledge_store_migrator", "store"))
				require.NoError(t, err)
				defer storeDB.Close()
				_, err = storeDB.ExecContext(ctx, "CREATE SCHEMA memory_store_probe")
				require.NoError(t, err, "the Store migrator creates its vendor schema")
				_, err = storeDB.ExecContext(ctx, "DROP SCHEMA memory_store_probe")
				require.NoError(t, err)
				_, err = storeDB.ExecContext(ctx, "SELECT count(*) FROM memory_facts")
				require.Error(t, err, "the Store migrator has no domain-table access")
				_, err = appDB.ExecContext(ctx, "CREATE SCHEMA forbidden")
				require.Error(t, err, "the app role creates no schema")
				// 00008 (P2.6): an upload capability binds its key to its
				// tenant and id and is consumed once; the app role never
				// deletes one.
				upload := func(id, key string) string {
					return "INSERT INTO source_uploads (upload_id, tenant_id, actor_id, command_id, request_digest, content_digest, size_bytes, media_type, object_key, expires_at) VALUES ('" + id + "', 't1', 'u1', 'cmd-" + id + "', '" + digest + "', '" + digest + "', 3, 'text/markdown', '" + key + "', now() + interval '15 minutes')"
				}
				id := "upl-" + strings.Repeat("0", 32)
				_, err = appDB.ExecContext(ctx, upload(id, "uploads/t1/"+id))
				require.NoError(t, err, "the app role records an upload capability")
				for stmt, why := range map[string]string{
					upload("upl-"+strings.Repeat("1", 32), "uploads/t2/upl-"+strings.Repeat("1", 32)): "the key is the tenant's and the upload's",
					upload("upload-x", "uploads/t1/upload-x"):                                         "an upload id has its shape",
					"UPDATE source_uploads SET consumed_at = now() WHERE upload_id = '" + id + "'":    "consumption names the source",
					"DELETE FROM source_uploads":                                                      "the app role never deletes an upload",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.Error(t, err, why)
				}
				_, err = appDB.ExecContext(ctx, "UPDATE source_uploads SET consumed_at = now(), source_id = 'src-m' WHERE upload_id = '"+id+"'")
				require.NoError(t, err, "consumed by its source")
				_, err = appDB.ExecContext(ctx, "UPDATE source_uploads SET consumed_at = NULL, source_id = NULL WHERE upload_id = '"+id+"'")
				require.Error(t, err, "a consumed upload is never reused")
				// 00011 (P2.7): the cleanup identity deletes delivery and
				// content rows only; the app role still deletes nothing.
				cleanDB, err := migrate.Open(dsn("anvilkit_knowledge_cleanup", "cleanup"))
				require.NoError(t, err)
				defer cleanDB.Close()
				_, err = cleanDB.ExecContext(ctx, "DELETE FROM chunks WHERE source_id = 'src-none'")
				require.NoError(t, err, "cleanup deletes chunk rows")
				_, err = cleanDB.ExecContext(ctx, "DELETE FROM inbox WHERE accepted_at < now() - interval '30 days'")
				require.NoError(t, err, "cleanup prunes the inbox")
				for stmt, why := range map[string]string{
					"DELETE FROM sources":              "cleanup never deletes a source",
					"DELETE FROM memory_facts":         "cleanup never touches memory",
					"UPDATE chunks SET locator = '{}'": "cleanup never rewrites content",
					"INSERT INTO inbox (event_id, consumer) VALUES (gen_random_uuid(), 'x')": "cleanup never writes deliveries",
				} {
					_, err = cleanDB.ExecContext(ctx, stmt)
					require.Error(t, err, why)
				}
				_, err = appDB.ExecContext(ctx, "DELETE FROM chunks")
				require.Error(t, err, "the app role never deletes chunk rows")
				// Clean the rows so Down can run.
				for _, stmt := range []string{"source_uploads", "memory_removals", "memory_projections", "background_requests", "memory_decisions", "memory_facts", "index_entries", "index_generations", "chunks", "ingest_requests", "source_commands", "source_acl_revisions", "source_revisions", "sources"} {
					_, err = db.ExecContext(ctx, "DELETE FROM "+stmt)
					require.NoError(t, err)
				}
			}

			if domain == "mcp" {
				// 00003: a descriptor records its discoverer and a different
				// reviewer; catalog commands are idempotent per (tenant,
				// command); an active grant carries Control's receipt; a
				// revoked grant carries a converged barrier; reviews and grant
				// decisions are append-only for the app role.
				d := "sha256:" + strings.Repeat("a", 64)
				for _, stmt := range []string{
					"INSERT INTO servers (server_id, tenant_id, canonical_resource, transport) VALUES ('srv-m', 't1', 'https://mcp.example/m', 'streamable-http')",
					"INSERT INTO descriptors (server_id, revision, protocol_version, provenance, descriptor_digest, tools, state, command_id, request_digest, data_class, revision_evidence, discovered_by) VALUES ('srv-m', 1, '2026-07-28', 'test', '" + d + "', '[]', 'review_pending', 'c1', '" + d + "', 'internal', 'cf:1', 'alice')",
					"INSERT INTO catalog_commands (tenant_id, command_id, command_kind, request_digest, server_id, revision) VALUES ('t1', 'c1', 'discover', '" + d + "', 'srv-m', 1)",
					"INSERT INTO reviews (review_id, server_id, revision, descriptor_digest, reviewer, decision, command_id, request_digest, tenant_id) VALUES ('rv1', 'srv-m', 1, '" + d + "', 'bob', 'approve', 'c2', '" + d + "', 't1')",
					"UPDATE descriptors SET state = 'approved', reviewer = 'bob', review_id = 'rv1' WHERE server_id = 'srv-m'",
					"INSERT INTO grants (grant_id, tenant_id, subject_type, subject_id, server_id, descriptor_revision, descriptor_digest, methods, purpose, cost_cap_currency, cost_cap_amount, state, command_id, request_digest, canonical_resource, transport, protocol_version, audience, data_class, policy_digest, registration_command_id) VALUES ('g-m', 't1', 'project', 'p1', 'srv-m', 1, '" + d + "', '{m}', 'x', 'USD', 1, 'pending', 'c3', '" + d + "', 'https://mcp.example/m', 'streamable-http', '2026-07-28', 'https://mcp.example/m', 'internal', '" + d + "', 'reg-g-m')",
					// 00004: a tool request binds its execution and argument bytes.
					"INSERT INTO tool_requests (call_id, tenant_id, grant_id, grant_revision, server_id, descriptor_revision, method, argument_ref, argument_digest, state, command_id, request_digest, deadline, instance_id, execution_epoch, arguments) VALUES ('call-m', 't1', 'g-m', 1, 'srv-m', 1, 'm', 'inline', '" + d + "', 'admitted', 'c4', '" + d + "', now(), 'i1', 1, '\\x7b7d')",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.NoError(t, err, stmt)
				}
				for stmt, why := range map[string]string{
					"UPDATE descriptors SET reviewer = 'alice' WHERE server_id = 'srv-m'":                                                                                            "the discoverer never reviews its own revision",
					"INSERT INTO catalog_commands (tenant_id, command_id, command_kind, request_digest, server_id, revision) VALUES ('t1', 'c1', 'review', '" + d + "', 'srv-m', 1)": "one catalog command per (tenant, command)",
					"UPDATE grants SET state = 'active' WHERE grant_id = 'g-m'":                                                                                                      "an active grant carries Control's receipt",
					"UPDATE grants SET state = 'revoking' WHERE grant_id = 'g-m'":                                                                                                    "a revoking grant names its revocation command",
					"UPDATE grants SET state = 'revoked', revocation_command_id = 'r1', revoked_at = now() WHERE grant_id = 'g-m'":                                                   "a revoked grant carries a converged barrier",
					"UPDATE reviews SET reviewer = 'eve'":                                                                                                                            "reviews are append-only",
					"DELETE FROM reviews":                                                                                                                                            "reviews are append-only",
					"UPDATE tool_requests SET state = 'sent' WHERE call_id = 'call-m'":                                                                                               "a sent call carries the send marker committed before its send",
					"UPDATE tool_requests SET arguments = decode(repeat('00', 65537), 'hex') WHERE call_id = 'call-m'":                                                               "argument bytes are bounded",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.Error(t, err, why)
				}
				// 00005 (P2.5 AC6): the production schema admits only the
				// business kind; the DEVELOPMENT_ONLY fixture set re-admits
				// local-check under its own version table, and rolls back first.
				request := func(id, kind string) string {
					return "INSERT INTO background_requests (task_id, generation, tenant_id, task_kind, input_digest, input, state, result_profile, authorization_ref, correlation_id) VALUES ('" + id + "', 1, 't1', '" + kind + "', '" + d + "', '{}', 'pending', 'p-v1', 'grant:g-m', 'c-" + id + "')"
				}
				_, err = appDB.ExecContext(ctx, request("lc-prod", "local-check"))
				require.Error(t, err, "the production schema refuses the fixture kind")
				_, err = appDB.ExecContext(ctx, request("refresh-1", "mcp-catalog-refresh"))
				require.NoError(t, err, "the business kind")
				// 00006: the owner's recorded refresh observation binds its
				// drift to its digests and is append-only; a descriptor's input
				// schemas are a JSON object.
				refresh := func(live, drift, reason string) string {
					return "INSERT INTO catalog_refreshes (task_id, generation, worker_id, server_id, revision, reviewed_digest, live_digest, drift, drift_reason, result_ref, result_digest) VALUES ('refresh-1', 1, 'w1', 'srv-m', 1, '" + d + "', " + live + ", " + drift + ", " + reason + ", 'mcp-catalog-refresh:refresh-1:1', '" + d + "')"
				}
				for stmt, why := range map[string]string{
					refresh("'sha256:"+strings.Repeat("b", 64)+"'", "false", "NULL"):        "a changed live digest is a drift",
					refresh("NULL", "true", "NULL"):                                         "a drift names its reason",
					"UPDATE descriptors SET input_schemas = '[]' WHERE server_id = 'srv-m'": "input schemas are an object",
				} {
					_, err = appDB.ExecContext(ctx, stmt)
					require.Error(t, err, why)
				}
				_, err = appDB.ExecContext(ctx, refresh("'"+d+"'", "false", "NULL"))
				require.NoError(t, err, "an unchanged live read")
				_, err = appDB.ExecContext(ctx, "UPDATE catalog_refreshes SET drift = true")
				require.Error(t, err, "recorded refreshes are append-only for the app role")
				var validated bool
				require.NoError(t, db.QueryRowContext(ctx, "SELECT convalidated FROM pg_constraint WHERE conname = 'background_requests_task_kind_check'").Scan(&validated))
				require.True(t, validated, "validated where no fixture row exists")
				fixtures, err := migrate.UpFixtures(ctx, db, domain)
				require.NoError(t, err)
				require.Equal(t, int64(1), fixtures)
				_, err = appDB.ExecContext(ctx, request("lc-dev", "local-check"))
				require.NoError(t, err, "the development fixture set re-admits local-check")
				version, err = migrate.Version(ctx, db, domain)
				require.NoError(t, err)
				require.Equal(t, latest[domain], version, "the fixture set keeps its own version table")
				require.NoError(t, migrate.DownFixtures(ctx, db, domain))
				_, err = appDB.ExecContext(ctx, request("lc-after", "local-check"))
				require.Error(t, err, "rolled back: refused again (a fixture row already held stays)")
				// 00007 (P2.8, MCP-14): re-applied over existing records, the
				// event sequence starts at the highest revision an earlier event
				// could carry; each event takes the next value as the app role.
				version, err = migrate.DownTo(ctx, db, domain, 6)
				require.NoError(t, err)
				require.Equal(t, int64(6), version)
				version, err = migrate.Up(ctx, db, domain)
				require.NoError(t, err)
				require.Equal(t, latest[domain], version)
				var seeded int64
				require.NoError(t, db.QueryRowContext(ctx, "SELECT revision FROM event_revisions WHERE aggregate_type = 'descriptor' AND aggregate_id = 'srv-m'").Scan(&seeded))
				var maxRevision int64
				require.NoError(t, db.QueryRowContext(ctx, "SELECT max(revision) FROM descriptors WHERE server_id = 'srv-m'").Scan(&maxRevision))
				require.Equal(t, maxRevision, seeded, "the descriptor sequence continues above its published revisions")
				require.NoError(t, db.QueryRowContext(ctx, "SELECT revision FROM event_revisions WHERE aggregate_type = 'grant' AND aggregate_id = 'g-m'").Scan(&seeded))
				require.NoError(t, db.QueryRowContext(ctx, "SELECT revision FROM grants WHERE grant_id = 'g-m'").Scan(&maxRevision))
				require.Equal(t, maxRevision, seeded)
				next := "INSERT INTO event_revisions (aggregate_type, aggregate_id, revision) VALUES ('grant', 'g-new', 1) ON CONFLICT (aggregate_type, aggregate_id) DO UPDATE SET revision = event_revisions.revision + 1 RETURNING revision"
				var r1, r2 int64
				require.NoError(t, appDB.QueryRowContext(ctx, next).Scan(&r1))
				require.NoError(t, appDB.QueryRowContext(ctx, next).Scan(&r2))
				require.Equal(t, []int64{1, 2}, []int64{r1, r2}, "strictly increasing per aggregate")
				_, err = appDB.ExecContext(ctx, "INSERT INTO event_revisions (aggregate_type, aggregate_id, revision) VALUES ('operation', 'x', 1)")
				require.Error(t, err, "only the catalog and grant aggregates have a sequence here")
				for _, stmt := range []string{"event_revisions", "catalog_refreshes", "background_requests", "tool_requests", "grants", "reviews", "catalog_commands", "descriptors", "servers"} {
					_, err = db.ExecContext(ctx, "DELETE FROM "+stmt)
					require.NoError(t, err)
				}
			}

			// Reversible: down to empty, up again lands on the same version.
			version, err = migrate.DownTo(ctx, db, domain, 0)
			require.NoError(t, err)
			require.Equal(t, int64(0), version)
			version, err = migrate.Up(ctx, db, domain)
			require.NoError(t, err)
			require.Equal(t, latest[domain], version)
		})
	}
}
