-- +goose Up
-- P23: memory deletions and revocations survive a point-in-time restore
-- (platform.md §6, execution.md §4, SEC-06). Every removal decision commits
-- one row here in its own transaction, and Knowledge then writes the
-- matching immutable record to the removal inventory in the independent DR
-- store (pending until that write is confirmed). A restore of this
-- database cannot erase a record: Knowledge lists the inventory from the
-- newest removal the database still knows (less a clock margin), and every
-- record without a row is a removal the restore erased; it is re-applied
-- under its original command identity ('restored') before memory serves.
-- A restored row may name a fact the restore erased altogether: the row is
-- then the tombstone that refuses a retried proposal of that fact. Rows
-- carry identities, revisions and digests, never content.
--
-- The inventory belongs to this database alone: its records live under the
-- scope id created here once. A restore of this database keeps the scope
-- (it predates every removal), while another database (a new environment,
-- a test lane on the same store) has its own and never reads these records.
CREATE TABLE memory_removal_scope (
    scope_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    singleton  boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton),
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO memory_removal_scope DEFAULT VALUES;

CREATE TABLE memory_removals (
    tenant_id      text NOT NULL CHECK (tenant_id <> ''),
    command_id     text NOT NULL CHECK (command_id <> ''),
    fact_id        text NOT NULL,
    decision       text NOT NULL CHECK (decision IN ('delete', 'revoke')),
    decider        text NOT NULL CHECK (decider <> ''),
    confirmer      text NOT NULL DEFAULT '',
    reason_code    text NOT NULL DEFAULT '',
    request_digest text NOT NULL,
    from_revision  bigint NOT NULL CHECK (from_revision >= 1),
    to_revision    bigint NOT NULL,
    recorded_at    timestamptz NOT NULL,
    inventory_key  text UNIQUE,
    state          text NOT NULL CHECK (state IN ('pending', 'recorded', 'restored')),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, command_id),
    CHECK (to_revision = from_revision + 1),
    CHECK ((decision = 'revoke') = (confirmer <> '')),
    CHECK (recorded_at = date_trunc('milliseconds', recorded_at)),
    CHECK (state = 'pending' OR inventory_key IS NOT NULL)
);
CREATE INDEX memory_removals_recorded_idx ON memory_removals (recorded_at);
CREATE INDEX memory_removals_fact_idx ON memory_removals (fact_id);
CREATE INDEX memory_removals_pending_idx ON memory_removals (updated_at) WHERE state = 'pending';

-- The removals decided before this migration are recorded too: pending,
-- the inventory key derived by Knowledge when it writes the record.
INSERT INTO memory_removals (tenant_id, command_id, fact_id, decision, decider, confirmer, reason_code, request_digest,
                             from_revision, to_revision, recorded_at, state)
SELECT d.tenant_id, d.command_id, d.fact_id, d.decision, d.decider,
       CASE WHEN d.decision = 'revoke' THEN f.confirmer ELSE '' END,
       CASE WHEN d.decision = 'revoke' THEN COALESCE(d.reason_code, '') ELSE '' END,
       d.request_digest, d.from_revision, d.to_revision, date_trunc('milliseconds', d.decided_at), 'pending'
FROM memory_decisions d
JOIN memory_facts f ON f.fact_id = d.fact_id
WHERE d.decision IN ('delete', 'revoke');

-- The runtime role records and marks removals; it never deletes one, and
-- it only reads the scope.
GRANT SELECT, INSERT, UPDATE ON memory_removals TO anvilkit_knowledge_app;
REVOKE ALL ON memory_removal_scope FROM anvilkit_knowledge_app;
GRANT SELECT ON memory_removal_scope TO anvilkit_knowledge_app;

-- +goose Down
DROP TABLE memory_removals;
DROP TABLE memory_removal_scope;
