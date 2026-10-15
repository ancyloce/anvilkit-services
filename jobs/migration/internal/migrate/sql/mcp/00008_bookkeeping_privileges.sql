-- P3.7 (plan 0004): the runtime role never writes migration bookkeeping.
-- 00001 granted SELECT, INSERT, UPDATE on every table, goose_db_version
-- included (goose creates it before 00001 runs); the role keeps SELECT, which
-- the replica's schema check reads.

-- +goose Up
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON goose_db_version FROM anvilkit_mcp_app;

-- +goose Down
GRANT INSERT, UPDATE ON goose_db_version TO anvilkit_mcp_app;
