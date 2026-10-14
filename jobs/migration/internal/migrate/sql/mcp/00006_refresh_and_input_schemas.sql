-- P2.5: the call-time argument check and the catalog refresh.
--
-- descriptors.input_schemas: each method's live input schema as discovery
-- read it (method -> JSON Schema). CreateCall validates a call's argument
-- bytes against the grant revision's document (CreateCallRequest.arguments);
-- its digest is the method's inputSchemaDigest, already covered by the
-- descriptor digest. A revision discovered before this migration has none:
-- its calls are refused (SCHEMA_UNAVAILABLE) until it is rediscovered.
--
-- catalog_refreshes: MCP's own recorded observation of one claimed
-- mcp-catalog-refresh generation (CatalogRefreshService.AdvanceRefresh):
-- the reviewed digest, the live digest (absent when the live server no
-- longer matches the declaration at all) and whether it drifted. Acceptance
-- compares the claimant's submission with result_ref/result_digest; an
-- accepted drift disables the revision. Append-only for the app role.

-- +goose Up
ALTER TABLE descriptors
    ADD COLUMN input_schemas jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(input_schemas) = 'object');

CREATE TABLE catalog_refreshes (
    task_id          text NOT NULL,
    generation       bigint NOT NULL,
    worker_id        text NOT NULL CHECK (worker_id <> ''),
    server_id        text NOT NULL,
    revision         bigint NOT NULL,
    reviewed_digest  text NOT NULL CHECK (reviewed_digest ~ '^sha256:[0-9a-f]{64}$'),
    live_digest      text CHECK (live_digest IS NULL OR live_digest ~ '^sha256:[0-9a-f]{64}$'),
    drift            boolean NOT NULL,
    drift_reason     text CHECK (drift_reason IS NULL OR octet_length(drift_reason) <= 1024),
    result_ref       text NOT NULL CHECK (result_ref <> ''),
    result_digest    text NOT NULL CHECK (result_digest ~ '^sha256:[0-9a-f]{64}$'),
    refreshed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, generation),
    FOREIGN KEY (task_id, generation) REFERENCES background_requests (task_id, generation),
    FOREIGN KEY (server_id, revision) REFERENCES descriptors (server_id, revision),
    CONSTRAINT catalog_refreshes_drift_check CHECK (drift = (live_digest IS DISTINCT FROM reviewed_digest)),
    CONSTRAINT catalog_refreshes_reason_check CHECK (drift = (drift_reason IS NOT NULL))
);
CREATE INDEX catalog_refreshes_server_idx ON catalog_refreshes (server_id, revision, refreshed_at);
GRANT SELECT, INSERT ON catalog_refreshes TO anvilkit_mcp_app;

-- +goose Down
REVOKE ALL ON catalog_refreshes FROM anvilkit_mcp_app;
DROP TABLE catalog_refreshes;
ALTER TABLE descriptors DROP COLUMN input_schemas;
