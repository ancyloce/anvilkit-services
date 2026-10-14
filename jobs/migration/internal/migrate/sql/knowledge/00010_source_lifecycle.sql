-- P2.7: the source lifecycle.
--
-- source_acl.permission: an entry grants read, or manage (read, replace the
-- ACL, delete). The registering actor owns the source (sources.owner_id,
-- backfilled from ACL revision 1, whose actor registered it); the owner or
-- a principal holding a manage entry changes access or deletes.
--
-- sources.purged_at: when the asynchronous erasure after a deletion removed
-- the upload, the content-addressed copy no live revision shares, the parse
-- results, the chunk rows and every point (points_purged_at stays the
-- Qdrant step's marker).

-- +goose Up
ALTER TABLE source_acl
    ADD COLUMN permission text NOT NULL DEFAULT 'read' CHECK (permission IN ('read', 'manage'));
ALTER TABLE sources
    ADD COLUMN owner_id  text,
    ADD COLUMN purged_at timestamptz,
    ADD CONSTRAINT sources_purged_check CHECK (purged_at IS NULL OR (deleted AND points_purged_at IS NOT NULL));
UPDATE sources s SET owner_id = r.actor_id
FROM source_acl_revisions r WHERE r.source_id = s.source_id AND r.acl_revision = 1 AND s.owner_id IS NULL;
CREATE INDEX sources_unpurged_idx ON sources (deleted_at) WHERE deleted AND purged_at IS NULL;

-- +goose Down
DROP INDEX sources_unpurged_idx;
ALTER TABLE sources
    DROP CONSTRAINT sources_purged_check,
    DROP COLUMN purged_at,
    DROP COLUMN owner_id;
ALTER TABLE source_acl DROP COLUMN permission;
