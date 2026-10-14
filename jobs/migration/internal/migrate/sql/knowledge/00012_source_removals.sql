-- P2.7: source deletions and ACL revocations survive a point-in-time restore
-- like memory removals (00007). The decision commits its row; its immutable
-- record is created in the removal inventory (the independent DR store)
-- before the caller is answered. A restore that erased the decision is
-- detected by the shared watermark and re-applied under the original
-- command: a deletion deletes again, a revocation installs the recorded
-- access list again. The app role records and marks rows, never deletes.

-- +goose Up
CREATE TABLE source_removals (
    tenant_id      text NOT NULL CHECK (tenant_id <> ''),
    command_id     text NOT NULL CHECK (command_id <> ''),
    source_id      text NOT NULL,
    decision       text NOT NULL CHECK (decision IN ('delete', 'revoke')),
    decider        text NOT NULL CHECK (decider <> ''),
    request_digest text NOT NULL,
    acl_revision   bigint NOT NULL CHECK (acl_revision >= 1),
    access         jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(access) = 'array'),
    recorded_at    timestamptz NOT NULL CHECK (recorded_at = date_trunc('milliseconds', recorded_at)),
    inventory_key  text UNIQUE,
    state          text NOT NULL CHECK (state IN ('pending', 'recorded', 'restored')),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, command_id),
    CHECK (decision = 'revoke' OR access = '[]'::jsonb),
    CHECK (state = 'pending' OR inventory_key IS NOT NULL)
);
CREATE INDEX source_removals_recorded_idx ON source_removals (recorded_at);
CREATE INDEX source_removals_pending_idx ON source_removals (updated_at) WHERE state = 'pending';
GRANT SELECT, INSERT, UPDATE ON source_removals TO anvilkit_knowledge_app;

-- +goose Down
REVOKE ALL ON source_removals FROM anvilkit_knowledge_app;
DROP TABLE source_removals;
