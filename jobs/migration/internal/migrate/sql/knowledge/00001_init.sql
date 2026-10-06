-- +goose Up
-- anvilkit_knowledge: Knowledge is the sole writer (contracts.md §3, DD-07).
-- Runs as anvilkit_knowledge_migrator; anvilkit_knowledge_app receives DML.
-- The PostgresStore projection lives in a separate vendor schema created by
-- its own qualified migration identity (P17); Qdrant is a rebuildable index.
-- The Watermill outbox physical schema arrives with its pinned adapter (P14).

CREATE TABLE sources (
    source_id        text PRIMARY KEY,
    tenant_id        text NOT NULL,
    project_id       text NOT NULL DEFAULT '',
    kind             text NOT NULL CHECK (kind IN ('document', 'url', 'repository', 'brand')),
    locator          text NOT NULL,
    current_revision bigint NOT NULL DEFAULT 1 CHECK (current_revision >= 1),
    acl_revision     bigint NOT NULL DEFAULT 1 CHECK (acl_revision >= 1),
    deleted          boolean NOT NULL DEFAULT false,
    command_id       text NOT NULL,
    request_digest   text NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, command_id)
);
CREATE INDEX sources_tenant_idx ON sources (tenant_id, project_id, created_at, source_id);

CREATE TABLE source_revisions (
    source_id      text NOT NULL REFERENCES sources (source_id),
    revision       bigint NOT NULL CHECK (revision >= 1),
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    media_type     text NOT NULL,
    size_bytes     bigint NOT NULL CHECK (size_bytes >= 0),
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, revision)
);

-- Trusted ACL: principals are verified identities; parsed content never writes here.
CREATE TABLE source_acl (
    source_id      text NOT NULL REFERENCES sources (source_id),
    acl_revision   bigint NOT NULL,
    principal_type text NOT NULL CHECK (principal_type IN ('tenant', 'project', 'actor', 'role')),
    principal_id   text NOT NULL,
    PRIMARY KEY (source_id, acl_revision, principal_type, principal_id)
);

CREATE TABLE ingest_requests (
    request_id      text PRIMARY KEY,
    source_id       text NOT NULL REFERENCES sources (source_id),
    source_revision bigint NOT NULL,
    state           text NOT NULL CHECK (state IN ('pending', 'parsing', 'indexing', 'indexed', 'failed', 'stale')),
    parser_profile  text NOT NULL,
    chunker_profile text NOT NULL,
    failure_code    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_id, source_revision, parser_profile, chunker_profile)
);

CREATE TABLE index_generations (
    generation       bigint PRIMARY KEY CHECK (generation >= 1),
    model_id         text NOT NULL,
    model_revision   text NOT NULL,
    chunker_profile  text NOT NULL,
    collection_name  text NOT NULL UNIQUE,
    state            text NOT NULL CHECK (state IN ('building', 'materialized', 'accepted', 'retired')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    accepted_at      timestamptz
);

-- chunkId binds source revision, parser/chunker profile and ordinal.
CREATE TABLE chunks (
    chunk_id        text PRIMARY KEY,
    source_id       text NOT NULL REFERENCES sources (source_id),
    source_revision bigint NOT NULL,
    parser_profile  text NOT NULL,
    chunker_profile text NOT NULL,
    ordinal         bigint NOT NULL CHECK (ordinal >= 0),
    locator         text NOT NULL,
    content_digest  text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    content_ref     text NOT NULL,
    quality_flags   text[] NOT NULL DEFAULT '{}',
    UNIQUE (source_id, source_revision, parser_profile, chunker_profile, ordinal)
);

CREATE TABLE snapshots (
    snapshot_id    text PRIMARY KEY,
    tenant_id      text NOT NULL,
    project_id     text NOT NULL DEFAULT '',
    generation     bigint NOT NULL REFERENCES index_generations (generation),
    content_digest text NOT NULL,
    command_id     text NOT NULL,
    request_digest text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, command_id)
);

CREATE TABLE snapshot_sources (
    snapshot_id     text NOT NULL REFERENCES snapshots (snapshot_id),
    source_id       text NOT NULL REFERENCES sources (source_id),
    source_revision bigint NOT NULL,
    PRIMARY KEY (snapshot_id, source_id)
);

CREATE TABLE memory_facts (
    fact_id        text PRIMARY KEY,
    tenant_id      text NOT NULL,
    subject_type   text NOT NULL,
    subject_id     text NOT NULL,
    scope_id       text NOT NULL DEFAULT '',
    content        text NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    state          text NOT NULL CHECK (state IN ('proposed', 'confirmed', 'rejected', 'revoked', 'expired')),
    revision       bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    proposer       text NOT NULL,
    confirmer      text,
    source_refs    text[] NOT NULL DEFAULT '{}',
    expires_at     timestamptz,
    command_id     text NOT NULL,
    request_digest text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, command_id)
);
CREATE INDEX memory_facts_subject_idx ON memory_facts (tenant_id, subject_type, subject_id, state);

-- Append-only decision history; a model proposal can never confirm itself.
CREATE TABLE memory_decisions (
    decision_id    text PRIMARY KEY,
    fact_id        text NOT NULL REFERENCES memory_facts (fact_id),
    from_revision  bigint NOT NULL,
    to_revision    bigint NOT NULL,
    decision       text NOT NULL CHECK (decision IN ('propose', 'confirm', 'reject', 'revoke', 'expire')),
    decider        text NOT NULL,
    policy_revision text,
    reason_code    text,
    command_id     text NOT NULL,
    request_digest text NOT NULL,
    decided_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fact_id, to_revision)
);

CREATE TABLE inbox (
    event_id    uuid NOT NULL,
    consumer    text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, consumer)
);

CREATE TABLE background_requests (
    task_id       text NOT NULL,
    generation    bigint NOT NULL CHECK (generation >= 1),
    task_kind     text NOT NULL CHECK (task_kind IN ('knowledge-ingest', 'knowledge-project', 'memory-project')),
    input_digest  text NOT NULL CHECK (input_digest ~ '^sha256:[0-9a-f]{64}$'),
    input         jsonb NOT NULL,
    state         text NOT NULL CHECK (state IN ('pending', 'leased', 'result_submitted', 'accepted', 'retry_scheduled', 'dead', 'stale', 'canceled')),
    worker_id     text,
    lease_until   timestamptz,
    attempt_count integer NOT NULL DEFAULT 0,
    result_ref    text,
    result_digest text,
    failure_code  text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, generation)
);
CREATE INDEX background_requests_pending_idx ON background_requests (created_at) WHERE state IN ('pending', 'retry_scheduled');

CREATE TABLE task_attempts (
    task_id     text NOT NULL,
    generation  bigint NOT NULL,
    ordinal     integer NOT NULL,
    worker_id   text NOT NULL,
    leased_at   timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz NOT NULL,
    outcome     text CHECK (outcome IN ('submitted', 'accepted', 'stale', 'expired', 'failed')),
    PRIMARY KEY (task_id, generation, ordinal),
    FOREIGN KEY (task_id, generation) REFERENCES background_requests (task_id, generation)
);

GRANT USAGE ON SCHEMA public TO anvilkit_knowledge_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO anvilkit_knowledge_app;

-- +goose Down
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anvilkit_knowledge_app;
DROP TABLE task_attempts;
DROP TABLE background_requests;
DROP TABLE inbox;
DROP TABLE memory_decisions;
DROP TABLE memory_facts;
DROP TABLE snapshot_sources;
DROP TABLE snapshots;
DROP TABLE chunks;
DROP TABLE index_generations;
DROP TABLE ingest_requests;
DROP TABLE source_acl;
DROP TABLE source_revisions;
DROP TABLE sources;
