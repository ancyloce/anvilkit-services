-- +goose Up
-- P15: authorized ingestion and isolated parsing (DD-07 §2, contracts.md §3).
--
-- A source revision binds the verified bytes Knowledge copied under a
-- content-addressed key (knowledge/sources/<tenant>/<sha256>): the parser
-- stages exactly that object, never the caller's upload. The trusted ACL
-- keeps its history per acl_revision; every source command (register,
-- access replacement, delete) is idempotent by (tenant, command) with its
-- request digest. The ingest request names the background task that
-- carries its parse and the exact parser/chunker revisions; the launch
-- ledger records one parser Job per claimed attempt, the create marker
-- before the single create request, the Job's UID and the verified output.
-- A chunk's identity binds source revision, parser and chunker profile
-- revisions and ordinal.
ALTER TABLE source_revisions ADD COLUMN object_key text NOT NULL DEFAULT '';
ALTER TABLE source_revisions ALTER COLUMN object_key DROP DEFAULT;

CREATE TABLE source_acl_revisions (
    source_id      text NOT NULL REFERENCES sources (source_id),
    acl_revision   bigint NOT NULL CHECK (acl_revision >= 1),
    actor_id       text NOT NULL,
    command_id     text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id, acl_revision)
);

CREATE TABLE source_commands (
    tenant_id      text NOT NULL,
    command_id     text NOT NULL,
    command_kind   text NOT NULL CHECK (command_kind IN ('register', 'update_access', 'delete')),
    request_digest text NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    source_id      text NOT NULL REFERENCES sources (source_id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, command_id)
);

ALTER TABLE sources ADD COLUMN deleted_at timestamptz;

ALTER TABLE ingest_requests
    ADD COLUMN task_id                 text NOT NULL DEFAULT '',
    ADD COLUMN parser_profile_revision bigint NOT NULL DEFAULT 1 CHECK (parser_profile_revision >= 1),
    ADD COLUMN chunker_revision        bigint NOT NULL DEFAULT 1 CHECK (chunker_revision >= 1),
    ADD COLUMN page_count              integer CHECK (page_count >= 0),
    ADD COLUMN chunk_count             integer CHECK (chunk_count >= 0),
    ADD COLUMN result_ref              text,
    ADD COLUMN result_digest           text CHECK (result_digest ~ '^sha256:[0-9a-f]{64}$');
ALTER TABLE ingest_requests
    ALTER COLUMN task_id DROP DEFAULT,
    ALTER COLUMN parser_profile_revision DROP DEFAULT,
    ALTER COLUMN chunker_revision DROP DEFAULT,
    DROP CONSTRAINT ingest_requests_source_id_source_revision_parser_profile_ch_key,
    ADD CONSTRAINT ingest_requests_identity UNIQUE (source_id, source_revision, parser_profile, parser_profile_revision, chunker_profile, chunker_revision),
    ADD CONSTRAINT ingest_requests_task UNIQUE (task_id);

-- One parser Job per claimed attempt of an ingest task. create_requested
-- is the marker written before the single create request; job_uid binds
-- deletion to the exact object; result_* are the bytes Knowledge read and
-- verified, never the Pod's own report.
CREATE TABLE parse_launches (
    task_id          text NOT NULL,
    generation       bigint NOT NULL,
    attempt          integer NOT NULL CHECK (attempt >= 1),
    launch_id        text NOT NULL UNIQUE,
    launch_key       text NOT NULL UNIQUE CHECK (launch_key ~ '^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$'),
    worker_id        text NOT NULL,
    profile_id       text NOT NULL,
    profile_revision bigint NOT NULL,
    input_digest     text NOT NULL CHECK (input_digest ~ '^sha256:[0-9a-f]{64}$'),
    deadline         timestamptz NOT NULL,
    state            text NOT NULL CHECK (state IN ('creating', 'running', 'completed', 'failed')),
    create_requested boolean NOT NULL DEFAULT false,
    job_uid          text,
    result_key       text,
    result_digest    text CHECK (result_digest ~ '^sha256:[0-9a-f]{64}$'),
    result_size      bigint CHECK (result_size >= 0),
    verdict          text CHECK (verdict IN ('parsed', 'rejected')),
    failure_code     text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, generation, attempt),
    FOREIGN KEY (task_id, generation) REFERENCES background_requests (task_id, generation),
    CHECK (state <> 'completed' OR (result_digest IS NOT NULL AND verdict IS NOT NULL))
);
CREATE INDEX parse_launches_open_idx ON parse_launches (updated_at) WHERE state IN ('creating', 'running');

ALTER TABLE chunks
    ADD COLUMN parser_profile_revision bigint NOT NULL DEFAULT 1 CHECK (parser_profile_revision >= 1),
    ADD COLUMN chunker_revision        bigint NOT NULL DEFAULT 1 CHECK (chunker_revision >= 1),
    ADD COLUMN ingest_request_id       text NOT NULL DEFAULT '' ;
ALTER TABLE chunks
    ALTER COLUMN parser_profile_revision DROP DEFAULT,
    ALTER COLUMN chunker_revision DROP DEFAULT,
    ALTER COLUMN ingest_request_id DROP DEFAULT,
    DROP CONSTRAINT chunks_source_id_source_revision_parser_profile_chunker_pro_key,
    ADD CONSTRAINT chunks_identity UNIQUE (source_id, source_revision, parser_profile, parser_profile_revision, chunker_profile, chunker_revision, ordinal);
CREATE INDEX chunks_request_idx ON chunks (ingest_request_id, ordinal);

GRANT SELECT, INSERT, UPDATE ON source_acl_revisions, source_commands, parse_launches TO anvilkit_knowledge_app;

-- +goose Down
REVOKE ALL ON source_acl_revisions, source_commands, parse_launches FROM anvilkit_knowledge_app;
DROP INDEX chunks_request_idx;
ALTER TABLE chunks
    DROP CONSTRAINT chunks_identity,
    ADD CONSTRAINT chunks_source_id_source_revision_parser_profile_chunker_pro_key UNIQUE (source_id, source_revision, parser_profile, chunker_profile, ordinal),
    DROP COLUMN ingest_request_id,
    DROP COLUMN chunker_revision,
    DROP COLUMN parser_profile_revision;
DROP TABLE parse_launches;
ALTER TABLE ingest_requests
    DROP CONSTRAINT ingest_requests_task,
    DROP CONSTRAINT ingest_requests_identity,
    ADD CONSTRAINT ingest_requests_source_id_source_revision_parser_profile_ch_key UNIQUE (source_id, source_revision, parser_profile, chunker_profile),
    DROP COLUMN result_digest,
    DROP COLUMN result_ref,
    DROP COLUMN chunk_count,
    DROP COLUMN page_count,
    DROP COLUMN chunker_revision,
    DROP COLUMN parser_profile_revision,
    DROP COLUMN task_id;
ALTER TABLE sources DROP COLUMN deleted_at;
DROP TABLE source_commands;
DROP TABLE source_acl_revisions;
ALTER TABLE source_revisions DROP COLUMN object_key;
