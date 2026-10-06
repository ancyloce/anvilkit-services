-- +goose Up
-- P16: index generations, the index ledger and hybrid retrieval (DD-07 §3/§4,
-- contracts.md §3).
--
-- An index generation is one model/content space: one physical Qdrant
-- collection (anvilkit-knowledge-<generation>) bound to the embedding
-- profile and model revision, its dimensions, its sparse profile and the
-- chunker it indexes. It is accepted (the stable alias moves to it) only
-- after every eligible source revision has an accepted entry and the
-- collection's point count equals the ledger's watermark. A retired
-- generation stays readable for the snapshots that name it; revocation and
-- deletion apply to every generation that still exists.
--
-- An index entry is the state of one source revision in one generation:
-- the knowledge-project task that writes it, the resumable batch watermark
-- (points written in ordinal order), the verified point count and the
-- digest of Knowledge's point manifest, which is the only acceptable
-- result of that task. A deleted source's points are purged from every
-- generation after readability ended; points_purged_at records completion.
ALTER TABLE index_generations
    ADD COLUMN embedding_profile text NOT NULL DEFAULT '',
    ADD COLUMN dimensions        integer NOT NULL DEFAULT 1 CHECK (dimensions >= 1),
    ADD COLUMN sparse_profile    text NOT NULL DEFAULT '',
    ADD COLUMN chunker_revision  bigint NOT NULL DEFAULT 1 CHECK (chunker_revision >= 1),
    ADD COLUMN expected_points   bigint NOT NULL DEFAULT 0 CHECK (expected_points >= 0),
    ADD COLUMN verified_points   bigint CHECK (verified_points >= 0),
    ADD COLUMN materialized_at   timestamptz,
    ADD COLUMN retired_at        timestamptz;
ALTER TABLE index_generations
    ALTER COLUMN embedding_profile DROP DEFAULT,
    ALTER COLUMN dimensions DROP DEFAULT,
    ALTER COLUMN sparse_profile DROP DEFAULT,
    ALTER COLUMN chunker_revision DROP DEFAULT,
    ADD CONSTRAINT index_generations_accepted CHECK (state <> 'accepted' OR accepted_at IS NOT NULL);
-- At most one generation is accepted (the alias target) at a time.
CREATE UNIQUE INDEX index_generations_one_accepted ON index_generations ((true)) WHERE state = 'accepted';

CREATE TABLE index_entries (
    generation        bigint NOT NULL REFERENCES index_generations (generation),
    source_id         text NOT NULL REFERENCES sources (source_id),
    source_revision   bigint NOT NULL,
    ingest_request_id text NOT NULL REFERENCES ingest_requests (request_id),
    task_id           text NOT NULL UNIQUE,
    state             text NOT NULL CHECK (state IN ('pending', 'running', 'materialized', 'accepted', 'failed', 'stale')),
    chunk_count       integer NOT NULL CHECK (chunk_count >= 0),
    written_through   integer NOT NULL DEFAULT 0 CHECK (written_through >= 0),
    point_count       integer CHECK (point_count >= 0),
    manifest_digest   text CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    failure_code      text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (generation, source_id, source_revision),
    CHECK (state NOT IN ('materialized', 'accepted') OR (manifest_digest IS NOT NULL AND point_count = chunk_count))
);
CREATE INDEX index_entries_source_idx ON index_entries (source_id, source_revision);

ALTER TABLE sources ADD COLUMN points_purged_at timestamptz;

GRANT SELECT, INSERT, UPDATE ON index_entries TO anvilkit_knowledge_app;

-- +goose Down
REVOKE ALL ON index_entries FROM anvilkit_knowledge_app;
ALTER TABLE sources DROP COLUMN points_purged_at;
DROP TABLE index_entries;
DROP INDEX index_generations_one_accepted;
ALTER TABLE index_generations
    DROP CONSTRAINT index_generations_accepted,
    DROP COLUMN retired_at,
    DROP COLUMN materialized_at,
    DROP COLUMN verified_points,
    DROP COLUMN expected_points,
    DROP COLUMN chunker_revision,
    DROP COLUMN sparse_profile,
    DROP COLUMN dimensions,
    DROP COLUMN embedding_profile;
