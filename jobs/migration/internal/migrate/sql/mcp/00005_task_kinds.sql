-- P2.5 (AC6): the production schema admits only the business kind of the
-- background lane, mcp-catalog-refresh. local-check, the DEVELOPMENT_ONLY
-- fixture kind, is re-admitted by the development fixture set
-- (sql/mcp-development, its own version table goose_db_version_development)
-- that this Job applies only under -development. A development foundation
-- may already hold fixture rows: the narrowed constraint is added NOT VALID
-- (every new row is checked) and validated wherever no fixture row exists.

-- +goose Up
ALTER TABLE background_requests
    DROP CONSTRAINT background_requests_task_kind_check,
    ADD CONSTRAINT background_requests_task_kind_check CHECK (task_kind IN ('mcp-catalog-refresh')) NOT VALID;
-- +goose StatementBegin
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM background_requests WHERE task_kind <> 'mcp-catalog-refresh') THEN
        ALTER TABLE background_requests VALIDATE CONSTRAINT background_requests_task_kind_check;
    END IF;
END $$;
-- +goose StatementEnd

-- +goose Down
ALTER TABLE background_requests
    DROP CONSTRAINT background_requests_task_kind_check,
    ADD CONSTRAINT background_requests_task_kind_check CHECK (task_kind IN ('mcp-catalog-refresh', 'local-check'));
