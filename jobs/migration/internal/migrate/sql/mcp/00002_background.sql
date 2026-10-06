-- +goose Up
-- P14: durable background work (DD-09 §1/§2, contracts.md §3).
--
-- outbox / outbox_offsets are the physical messages/offsets schema of the
-- pinned watermill-sql v4.1.5 PostgreSQL adapter (DefaultPostgreSQLSchema /
-- DefaultPostgreSQLOffsetsAdapter with the table names generated as
-- "outbox" and "outbox_offsets"): offset BIGSERIAL, uuid VARCHAR(36) (a
-- standard UUID; business identifiers travel in the envelope), payload and
-- metadata JSON, transaction_id xid8 from pg_current_xact_id(), ordered by
-- (transaction_id, offset) under the pg_snapshot_xmin visibility watermark.
-- MCP (Go) binds the pinned publisher to the same pgx transaction; its
-- in-process forwarder module reads messages and owns the subscriber
-- offsets; runtime schema initialization is disabled.
CREATE TABLE outbox (
    "offset"         BIGSERIAL,
    "uuid"           VARCHAR(36) NOT NULL,
    "created_at"     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "payload"        JSON DEFAULT NULL,
    "metadata"       JSON DEFAULT NULL,
    "transaction_id" xid8 NOT NULL,
    PRIMARY KEY ("transaction_id", "offset")
);

CREATE TABLE outbox_offsets (
    consumer_group                VARCHAR(255) NOT NULL,
    offset_acked                  BIGINT,
    last_processed_transaction_id xid8 NOT NULL,
    PRIMARY KEY (consumer_group)
);

-- The durable request carries what acceptance binds (DD-09 §1): the tenant
-- of its events, a revision that every mutation advances (aggregateRevision
-- of background.requested/completed), the result profile the submitted
-- result must satisfy, whether the computation is reconstructible (lease
-- expiry may reassign) or may have caused an external effect (the owner
-- asks Control about dispatch_id first), the owned authorization it is
-- rechecked against at acceptance (grant:<grant_id>), its retry bound and
-- schedule, and the correlation of its events. local-check is the fixture
-- kind of the background lane (DEVELOPMENT_ONLY computation, no business).
ALTER TABLE background_requests
    ADD COLUMN tenant_id         text NOT NULL DEFAULT '',
    ADD COLUMN revision          bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    ADD COLUMN result_profile    text NOT NULL DEFAULT '',
    ADD COLUMN effects           text NOT NULL DEFAULT 'reconstructible' CHECK (effects IN ('reconstructible', 'external')),
    ADD COLUMN dispatch_id       text,
    ADD COLUMN authorization_ref text NOT NULL DEFAULT '',
    ADD COLUMN max_attempts      integer NOT NULL DEFAULT 1 CHECK (max_attempts >= 1),
    ADD COLUMN retry_at          timestamptz,
    ADD COLUMN correlation_id    text NOT NULL DEFAULT '',
    ADD COLUMN completed_at      timestamptz;
ALTER TABLE background_requests
    ALTER COLUMN tenant_id DROP DEFAULT,
    ALTER COLUMN result_profile DROP DEFAULT,
    ALTER COLUMN authorization_ref DROP DEFAULT,
    ALTER COLUMN correlation_id DROP DEFAULT,
    ADD CONSTRAINT background_requests_tenant_id_check CHECK (tenant_id <> ''),
    ADD CONSTRAINT background_requests_result_profile_check CHECK (result_profile <> ''),
    ADD CONSTRAINT background_requests_authorization_ref_check CHECK (authorization_ref ~ '^grant:[A-Za-z0-9][A-Za-z0-9._:-]*$'),
    ADD CONSTRAINT background_requests_correlation_id_check CHECK (correlation_id <> ''),
    ADD CONSTRAINT background_requests_dispatch_check CHECK (effects = 'reconstructible' OR dispatch_id IS NOT NULL),
    DROP CONSTRAINT background_requests_task_kind_check,
    ADD CONSTRAINT background_requests_task_kind_check CHECK (task_kind IN ('mcp-catalog-refresh', 'local-check'));
CREATE INDEX background_requests_leased_idx ON background_requests (lease_until) WHERE state = 'leased';
CREATE INDEX background_requests_tenant_idx ON background_requests (tenant_id, created_at);

-- One claim per (generation, worker identity): a reused identifier cannot
-- take the same generation twice, so a later claim always fences the
-- earlier claimant.
ALTER TABLE task_attempts
    ADD COLUMN submitted_at timestamptz,
    ADD CONSTRAINT task_attempts_worker_once UNIQUE (task_id, generation, worker_id);

-- The inbox keeps the outcome a consumer committed for an event so a
-- redelivered duplicate returns it, and the aggregate revision it carried
-- so duplicates and gaps are measurable.
ALTER TABLE inbox
    ADD COLUMN outcome            text NOT NULL DEFAULT '',
    ADD COLUMN aggregate_revision bigint NOT NULL DEFAULT 0;

GRANT SELECT, INSERT, UPDATE ON background_requests, task_attempts, inbox TO anvilkit_mcp_app;
GRANT SELECT, INSERT ON outbox TO anvilkit_mcp_app;
GRANT USAGE ON SEQUENCE outbox_offset_seq TO anvilkit_mcp_app;
-- MCP's forwarder is a module of the service (DD-09 §2 "Control/MCP Go relay
-- can be service modules"): the app role also owns the subscriber offsets.
GRANT SELECT, INSERT, UPDATE ON outbox_offsets TO anvilkit_mcp_app;
-- The owner queue relay sidecar: reads durable requests to enqueue and
-- reconstruct, commits its inbox entries before ACK; nothing else.
GRANT SELECT ON background_requests TO anvilkit_mcp_relay;
GRANT SELECT, INSERT ON inbox TO anvilkit_mcp_relay;

-- +goose Down
REVOKE ALL ON background_requests, inbox FROM anvilkit_mcp_relay;
REVOKE ALL ON outbox, outbox_offsets FROM anvilkit_mcp_app;
ALTER TABLE inbox DROP COLUMN aggregate_revision, DROP COLUMN outcome;
ALTER TABLE task_attempts DROP CONSTRAINT task_attempts_worker_once, DROP COLUMN submitted_at;
DROP INDEX background_requests_tenant_idx;
DROP INDEX background_requests_leased_idx;
ALTER TABLE background_requests
    DROP CONSTRAINT background_requests_task_kind_check,
    ADD CONSTRAINT background_requests_task_kind_check CHECK (task_kind IN ('mcp-catalog-refresh')),
    DROP CONSTRAINT background_requests_dispatch_check,
    DROP CONSTRAINT background_requests_correlation_id_check,
    DROP CONSTRAINT background_requests_authorization_ref_check,
    DROP CONSTRAINT background_requests_result_profile_check,
    DROP CONSTRAINT background_requests_tenant_id_check,
    DROP COLUMN completed_at,
    DROP COLUMN correlation_id,
    DROP COLUMN retry_at,
    DROP COLUMN max_attempts,
    DROP COLUMN authorization_ref,
    DROP COLUMN dispatch_id,
    DROP COLUMN effects,
    DROP COLUMN result_profile,
    DROP COLUMN revision,
    DROP COLUMN tenant_id;
DROP TABLE outbox_offsets;
DROP TABLE outbox;
