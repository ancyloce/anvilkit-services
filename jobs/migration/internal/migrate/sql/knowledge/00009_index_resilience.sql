-- P2.7: index resilience. A failed index entry of a revision that is still
-- eligible is retried as a new task generation (attempts, failed_at) up to
-- the reviewed bound and then quarantined: a quarantined revision holds no
-- points in that generation and no longer blocks its acceptance (it is
-- counted and surfaced instead).

-- +goose Up
ALTER TABLE index_entries
    ADD COLUMN attempts  integer NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    ADD COLUMN failed_at timestamptz,
    DROP CONSTRAINT index_entries_state_check,
    ADD CONSTRAINT index_entries_state_check CHECK (state IN ('pending', 'running', 'materialized', 'accepted', 'failed', 'stale', 'quarantined')),
    ADD CONSTRAINT index_entries_quarantine_check CHECK (state <> 'quarantined' OR failure_code IS NOT NULL);
CREATE INDEX index_entries_failed_idx ON index_entries (generation, failed_at) WHERE state = 'failed';

-- +goose Down
DROP INDEX index_entries_failed_idx;
UPDATE index_entries SET state = 'failed' WHERE state = 'quarantined';
ALTER TABLE index_entries
    DROP CONSTRAINT index_entries_quarantine_check,
    DROP CONSTRAINT index_entries_state_check,
    ADD CONSTRAINT index_entries_state_check CHECK (state IN ('pending', 'running', 'materialized', 'accepted', 'failed', 'stale')),
    DROP COLUMN failed_at,
    DROP COLUMN attempts;
