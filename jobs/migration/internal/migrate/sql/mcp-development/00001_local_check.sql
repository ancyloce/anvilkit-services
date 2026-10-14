-- DEVELOPMENT_ONLY fixture set of anvilkit_mcp (P2.5 AC6): applied by the
-- migration Job only under -development, after the domain's own set, with
-- its own version table (goose_db_version_development). It re-admits the
-- local-check fixture kind of the background lane that the production
-- schema (mcp 00005) no longer admits. A later production migration that
-- changes background_requests_task_kind_check must keep this set's result
-- in view.

-- +goose Up
ALTER TABLE background_requests
    DROP CONSTRAINT background_requests_task_kind_check,
    ADD CONSTRAINT background_requests_task_kind_check CHECK (task_kind IN ('mcp-catalog-refresh', 'local-check'));

-- +goose Down
ALTER TABLE background_requests
    DROP CONSTRAINT background_requests_task_kind_check,
    ADD CONSTRAINT background_requests_task_kind_check CHECK (task_kind IN ('mcp-catalog-refresh')) NOT VALID;
