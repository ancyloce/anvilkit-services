-- +goose Up
-- P2.8 (MCP-14): the event sequence of each aggregate. aggregateRevision
-- of mcp.catalog-reviewed (aggregate descriptor = the server) and
-- mcp.grant-revoked (aggregate grant) strictly increases over every event
-- of the aggregate: APPROVED then DISABLED of one descriptor revision, and
-- REVOKING then REVOKED of one grant revision, used to share a revision,
-- so a consumer that rejects non-increasing revisions dropped the second.
-- The business revision stays in the payload. Each event takes the next
-- value on its own transaction (lock rank 7, after the aggregate's row).
-- The seed is the highest revision an earlier event could have carried
-- (the descriptor's or the grant's own revision), so the sequence
-- continues above every published one.
CREATE TABLE event_revisions (
    aggregate_type text NOT NULL CHECK (aggregate_type IN ('descriptor', 'grant')),
    aggregate_id   text NOT NULL,
    revision       bigint NOT NULL CHECK (revision >= 1),
    PRIMARY KEY (aggregate_type, aggregate_id)
);
INSERT INTO event_revisions (aggregate_type, aggregate_id, revision)
    SELECT 'descriptor', server_id, max(revision) FROM descriptors GROUP BY server_id;
INSERT INTO event_revisions (aggregate_type, aggregate_id, revision)
    SELECT 'grant', grant_id, revision FROM grants;
GRANT SELECT, INSERT, UPDATE ON event_revisions TO anvilkit_mcp_app;

-- +goose Down
REVOKE ALL ON event_revisions FROM anvilkit_mcp_app;
DROP TABLE event_revisions;
