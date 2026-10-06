-- +goose Up
-- P19: guarded tool execution (DD-08 §3-§5). A tool request binds the whole
-- execution (operation, attempt, instance, epoch), the exact argument bytes
-- (bounded; their digest is argument_digest), and, at acceptance, the grant
-- revision's descriptor revision/digest, protocol version, transport and the
-- qualified route. send_marker_at is committed before the single physical
-- send: a request that is admitted but carries no marker was provably never
-- sent; one that carries the marker without a recorded outcome is UNKNOWN.
-- native_evidence is the bounded private JSON-RPC result, result the
-- normalized typed result; both are private evidence of the tenant.
-- observation_sequence is the next sequence of this sender's observations
-- to Control. Rows from before this migration keep empty bindings.
ALTER TABLE tool_requests
    ADD COLUMN instance_id           text NOT NULL DEFAULT '',
    ADD COLUMN execution_epoch       bigint NOT NULL DEFAULT 0,
    ADD COLUMN arguments             bytea NOT NULL DEFAULT '' CHECK (octet_length(arguments) <= 65536),
    ADD COLUMN descriptor_digest     text NOT NULL DEFAULT '',
    ADD COLUMN protocol_version      text NOT NULL DEFAULT '',
    ADD COLUMN transport             text NOT NULL DEFAULT '',
    ADD COLUMN route                 text NOT NULL DEFAULT '',
    ADD COLUMN side_effecting        boolean NOT NULL DEFAULT false,
    ADD COLUMN exposure_currency     text NOT NULL DEFAULT '',
    ADD COLUMN exposure_amount       bigint NOT NULL DEFAULT 0 CHECK (exposure_amount >= 0),
    ADD COLUMN send_marker_at        timestamptz,
    ADD COLUMN observed_at           timestamptz,
    ADD COLUMN observation_sequence  bigint NOT NULL DEFAULT 1,
    ADD COLUMN usage_units           bigint,
    ADD COLUMN native_evidence       bytea CHECK (native_evidence IS NULL OR octet_length(native_evidence) <= 1048576),
    ADD COLUMN result                bytea CHECK (result IS NULL OR octet_length(result) <= 1048576);
ALTER TABLE tool_requests
    ADD CONSTRAINT tool_requests_marker_check CHECK (state NOT IN ('sent', 'succeeded', 'unknown') OR send_marker_at IS NOT NULL),
    ADD CONSTRAINT tool_requests_evidence_check CHECK (state NOT IN ('succeeded', 'failed') OR result IS NULL OR result_digest IS NOT NULL);
-- The uncertain-recovery restriction: an unresolved UNKNOWN call of a
-- tenant blocks new calls to that server.
CREATE INDEX tool_requests_unknown_idx ON tool_requests (tenant_id, server_id) WHERE state = 'unknown';
-- The reconciler: calls not yet terminal, oldest first.
CREATE INDEX tool_requests_open_idx ON tool_requests (updated_at) WHERE state IN ('accepted', 'admitted', 'sent', 'unknown');

-- +goose Down
DROP INDEX tool_requests_open_idx;
DROP INDEX tool_requests_unknown_idx;
ALTER TABLE tool_requests
    DROP CONSTRAINT tool_requests_evidence_check,
    DROP CONSTRAINT tool_requests_marker_check,
    DROP COLUMN result,
    DROP COLUMN native_evidence,
    DROP COLUMN usage_units,
    DROP COLUMN observation_sequence,
    DROP COLUMN observed_at,
    DROP COLUMN send_marker_at,
    DROP COLUMN exposure_amount,
    DROP COLUMN exposure_currency,
    DROP COLUMN side_effecting,
    DROP COLUMN route,
    DROP COLUMN transport,
    DROP COLUMN protocol_version,
    DROP COLUMN descriptor_digest,
    DROP COLUMN arguments,
    DROP COLUMN execution_epoch,
    DROP COLUMN instance_id;
