-- +goose Up
-- P17: confirmed long-term memory, its projections and deletion (DD-07 §5,
-- contracts.md §3, SEC-06).
--
-- A MemoryFact records who its proposal speaks for (origin: a user, a model
-- or a worker; models and workers only propose), its exact provenance
-- (<source_id>@<revision>), its subject (actor, project or tenant) and scope.
-- Deletion ends readability in its own transaction: the content is erased,
-- the digest and the append-only decision history stay as minimal audit
-- evidence, and purged_at records that every projection target has applied
-- the tombstone. At most one live confirmed fact carries the same content
-- for the same subject and scope (the conflict rule of a confirmation).
ALTER TABLE memory_facts
    ADD COLUMN origin     text NOT NULL DEFAULT 'user' CHECK (origin IN ('user', 'model', 'worker')),
    ADD COLUMN deleted    boolean NOT NULL DEFAULT false,
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN purged_at  timestamptz,
    ADD CONSTRAINT memory_facts_subject_type_check CHECK (subject_type IN ('actor', 'project', 'tenant')),
    ADD CONSTRAINT memory_facts_deleted_check CHECK (NOT deleted OR (deleted_at IS NOT NULL AND content = '')),
    ADD CONSTRAINT memory_facts_confirmer_check CHECK (state NOT IN ('confirmed', 'revoked', 'expired') OR confirmer IS NOT NULL);
ALTER TABLE memory_facts ALTER COLUMN origin DROP DEFAULT;
CREATE UNIQUE INDEX memory_facts_one_confirmed ON memory_facts (tenant_id, scope_id, subject_type, subject_id, content_digest)
    WHERE state = 'confirmed' AND NOT deleted;
CREATE INDEX memory_facts_proposer_idx ON memory_facts (tenant_id, proposer, origin);
CREATE INDEX memory_facts_expiry_idx ON memory_facts (expires_at) WHERE state = 'confirmed' AND NOT deleted AND expires_at IS NOT NULL;

-- Every decision (the proposal included) is one append-only row, idempotent
-- by (tenant, command): the authority that took it (a user, the proposing
-- model or worker for the proposal only, or a reviewed rule with its
-- policy revision), the provenance it bound and the expiry it set.
ALTER TABLE memory_decisions
    ADD COLUMN tenant_id   text NOT NULL DEFAULT '',
    ADD COLUMN authority   text NOT NULL DEFAULT 'user' CHECK (authority IN ('user', 'model', 'worker', 'policy')),
    ADD COLUMN source_refs text[] NOT NULL DEFAULT '{}',
    ADD COLUMN expires_at  timestamptz,
    DROP CONSTRAINT memory_decisions_decision_check,
    ADD CONSTRAINT memory_decisions_decision_check CHECK (decision IN ('propose', 'confirm', 'reject', 'revoke', 'expire', 'delete')),
    ADD CONSTRAINT memory_decisions_policy_check CHECK (authority <> 'policy' OR policy_revision IS NOT NULL),
    ADD CONSTRAINT memory_decisions_proposal_check CHECK (authority IN ('user', 'policy') OR decision = 'propose'),
    ADD CONSTRAINT memory_decisions_command UNIQUE (tenant_id, command_id);
ALTER TABLE memory_decisions
    ALTER COLUMN tenant_id DROP DEFAULT,
    ALTER COLUMN authority DROP DEFAULT,
    ADD CONSTRAINT memory_decisions_tenant_check CHECK (tenant_id <> '');

-- The projection ledger: one row per fact and target (0 is the
-- PostgresStore, g >= 1 the vector collection of index generation g). A
-- row names the fact revision and action (apply the confirmed content or
-- remove it) its memory-project task carries, the epoch a rebuild or retry
-- advanced, and the digest of the verified application: the only result
-- acceptance takes.
CREATE TABLE memory_projections (
    fact_id         text NOT NULL REFERENCES memory_facts (fact_id),
    target          bigint NOT NULL CHECK (target >= 0),
    fact_revision   bigint NOT NULL CHECK (fact_revision >= 1),
    action          text NOT NULL CHECK (action IN ('apply', 'remove')),
    epoch           integer NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    task_id         text NOT NULL UNIQUE,
    state           text NOT NULL CHECK (state IN ('pending', 'running', 'materialized', 'accepted', 'failed', 'stale')),
    manifest_digest text CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    failure_code    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fact_id, target),
    CHECK (state NOT IN ('materialized', 'accepted') OR manifest_digest IS NOT NULL)
);
CREATE INDEX memory_projections_target_idx ON memory_projections (target, state);

-- memory-project requests are authorized by their fact (memory:<fact_id>).
ALTER TABLE background_requests
    DROP CONSTRAINT background_requests_authorization_ref_check,
    ADD CONSTRAINT background_requests_authorization_ref_check CHECK (authorization_ref ~ '^(source|memory):[A-Za-z0-9][A-Za-z0-9._:-]*$');

GRANT SELECT, INSERT, UPDATE ON memory_projections TO anvilkit_knowledge_app;
-- Decisions are append-only for the runtime role.
REVOKE UPDATE ON memory_decisions FROM anvilkit_knowledge_app;

-- The PostgresStore projection lives in its own vendor schema, created and
-- migrated by the Store's own migrations under a separate identity
-- (anvilkit_knowledge_store_migrator, Knowledge's store-migrate entry); the
-- runtime Store role receives DML there only. This domain migration only
-- lets that identity create its schema in this database.
-- +goose StatementBegin
DO $$
BEGIN
    EXECUTE format('GRANT CREATE ON DATABASE %I TO anvilkit_knowledge_store_migrator', current_database());
END
$$;
-- +goose StatementEnd

-- +goose Down
-- +goose StatementBegin
DO $$
BEGIN
    EXECUTE format('REVOKE CREATE ON DATABASE %I FROM anvilkit_knowledge_store_migrator', current_database());
END
$$;
-- +goose StatementEnd
GRANT UPDATE ON memory_decisions TO anvilkit_knowledge_app;
REVOKE ALL ON memory_projections FROM anvilkit_knowledge_app;
ALTER TABLE background_requests
    DROP CONSTRAINT background_requests_authorization_ref_check,
    ADD CONSTRAINT background_requests_authorization_ref_check CHECK (authorization_ref ~ '^source:[A-Za-z0-9][A-Za-z0-9._:-]*$');
DROP TABLE memory_projections;
ALTER TABLE memory_decisions
    DROP CONSTRAINT memory_decisions_tenant_check,
    DROP CONSTRAINT memory_decisions_command,
    DROP CONSTRAINT memory_decisions_proposal_check,
    DROP CONSTRAINT memory_decisions_policy_check,
    DROP CONSTRAINT memory_decisions_decision_check,
    ADD CONSTRAINT memory_decisions_decision_check CHECK (decision IN ('propose', 'confirm', 'reject', 'revoke', 'expire')),
    DROP COLUMN expires_at,
    DROP COLUMN source_refs,
    DROP COLUMN authority,
    DROP COLUMN tenant_id;
DROP INDEX memory_facts_expiry_idx;
DROP INDEX memory_facts_proposer_idx;
DROP INDEX memory_facts_one_confirmed;
ALTER TABLE memory_facts
    DROP CONSTRAINT memory_facts_confirmer_check,
    DROP CONSTRAINT memory_facts_deleted_check,
    DROP CONSTRAINT memory_facts_subject_type_check,
    DROP COLUMN purged_at,
    DROP COLUMN deleted_at,
    DROP COLUMN deleted,
    DROP COLUMN origin;
