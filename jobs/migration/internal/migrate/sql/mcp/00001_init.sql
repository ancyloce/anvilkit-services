-- +goose Up
-- anvilkit_mcp: MCP is the sole writer (contracts.md §3, DD-08). Runs as
-- anvilkit_mcp_migrator; anvilkit_mcp_app receives DML. Upstream tokens never
-- live in these tables. Control keeps the grant policy projection and the
-- dispatch records; the Watermill outbox physical schema arrives with P14.

CREATE TABLE servers (
    server_id          text PRIMARY KEY,
    tenant_id          text NOT NULL,
    canonical_resource text NOT NULL,
    transport          text NOT NULL CHECK (transport IN ('streamable-http', 'stdio')),
    current_revision   bigint NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, canonical_resource)
);

CREATE TABLE descriptors (
    server_id         text NOT NULL REFERENCES servers (server_id),
    revision          bigint NOT NULL CHECK (revision >= 1),
    protocol_version  text NOT NULL,
    provenance        text NOT NULL,
    descriptor_digest text NOT NULL CHECK (descriptor_digest ~ '^sha256:[0-9a-f]{64}$'),
    tools             jsonb NOT NULL,
    state             text NOT NULL CHECK (state IN ('discovered', 'review_pending', 'approved', 'rejected', 'disabled')),
    command_id        text NOT NULL,
    request_digest    text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, revision),
    UNIQUE (server_id, descriptor_digest)
);

CREATE TABLE reviews (
    review_id         text PRIMARY KEY,
    server_id         text NOT NULL,
    revision          bigint NOT NULL,
    descriptor_digest text NOT NULL,
    reviewer          text NOT NULL,
    decision          text NOT NULL CHECK (decision IN ('approve', 'reject', 'disable')),
    reason_code       text,
    command_id        text NOT NULL,
    request_digest    text NOT NULL,
    decided_at        timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (server_id, revision) REFERENCES descriptors (server_id, revision),
    UNIQUE (server_id, command_id)
);

CREATE TABLE grants (
    grant_id            text PRIMARY KEY,
    revision            bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    tenant_id           text NOT NULL,
    subject_type        text NOT NULL CHECK (subject_type IN ('tenant', 'project', 'actor', 'role')),
    subject_id          text NOT NULL,
    server_id           text NOT NULL,
    descriptor_revision bigint NOT NULL,
    descriptor_digest   text NOT NULL,
    methods             text[] NOT NULL,
    purpose             text NOT NULL,
    cost_cap_currency   text NOT NULL,
    cost_cap_amount     bigint NOT NULL CHECK (cost_cap_amount >= 0),
    state               text NOT NULL CHECK (state IN ('pending', 'active', 'revoking', 'revoked', 'expired', 'registration_failed')),
    control_receipt_id  text,
    expires_at          timestamptz,
    command_id          text NOT NULL,
    request_digest      text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (server_id, descriptor_revision) REFERENCES descriptors (server_id, revision),
    UNIQUE (tenant_id, command_id)
);
CREATE INDEX grants_subject_idx ON grants (tenant_id, subject_type, subject_id, state);

CREATE TABLE grant_decisions (
    decision_id    text PRIMARY KEY,
    grant_id       text NOT NULL REFERENCES grants (grant_id),
    from_revision  bigint NOT NULL,
    to_revision    bigint NOT NULL,
    decision       text NOT NULL CHECK (decision IN ('create', 'activate', 'registration_failed', 'revoke', 'converge', 'expire')),
    decider        text NOT NULL,
    reason_code    text,
    command_id     text NOT NULL,
    request_digest text NOT NULL,
    decided_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (grant_id, to_revision)
);

CREATE TABLE tool_requests (
    call_id             text PRIMARY KEY,
    tenant_id           text NOT NULL,
    grant_id            text NOT NULL REFERENCES grants (grant_id),
    grant_revision      bigint NOT NULL,
    server_id           text NOT NULL,
    descriptor_revision bigint NOT NULL,
    method              text NOT NULL,
    argument_ref        text NOT NULL,
    argument_digest     text NOT NULL CHECK (argument_digest ~ '^sha256:[0-9a-f]{64}$'),
    operation_id        text,
    attempt_id          text,
    state               text NOT NULL CHECK (state IN ('accepted', 'admitted', 'sent', 'succeeded', 'failed', 'denied', 'unknown', 'canceled')),
    control_dispatch_id text,
    result_ref          text,
    result_digest       text,
    failure_code        text,
    command_id          text NOT NULL,
    request_digest      text NOT NULL,
    deadline            timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, command_id)
);
CREATE INDEX tool_requests_grant_open_idx ON tool_requests (grant_id, grant_revision) WHERE state IN ('accepted', 'admitted', 'sent', 'unknown');

CREATE TABLE inbox (
    event_id    uuid NOT NULL,
    consumer    text NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, consumer)
);

CREATE TABLE background_requests (
    task_id       text NOT NULL,
    generation    bigint NOT NULL CHECK (generation >= 1),
    task_kind     text NOT NULL CHECK (task_kind IN ('mcp-catalog-refresh')),
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

GRANT USAGE ON SCHEMA public TO anvilkit_mcp_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO anvilkit_mcp_app;

-- +goose Down
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anvilkit_mcp_app;
DROP TABLE task_attempts;
DROP TABLE background_requests;
DROP TABLE inbox;
DROP TABLE tool_requests;
DROP TABLE grant_decisions;
DROP TABLE grants;
DROP TABLE reviews;
DROP TABLE descriptors;
DROP TABLE servers;
