-- P3.7 (plan 0004): MCP bounds its delivery tables as Control does (Control's
-- 00017 grants DELETE on its outbox to its app role): the runtime role
-- deletes outbox rows the forwarder acknowledged, inbox entries past their
-- deduplication window and the attempts of terminal requests, a bounded batch
-- every retention interval (services/agent/mcp internal/adapters/outbox
-- Retain). No business table gains DELETE.

-- +goose Up
GRANT DELETE ON outbox, inbox, task_attempts TO anvilkit_mcp_app;
CREATE INDEX inbox_accepted_idx ON inbox (accepted_at);

-- +goose Down
DROP INDEX inbox_accepted_idx;
REVOKE DELETE ON outbox, inbox, task_attempts FROM anvilkit_mcp_app;
