-- +goose Up
-- P18: the private catalog and the grant barrier (DD-08 §1/§2, contracts.md §3).
--
-- A descriptor revision binds what discovery read from the live server
-- through the connection layer (server name/version, protocol, tool schemas
-- and annotations, resource and prompt listings) and what the listing
-- declared and the review covers (per-method side effects, price,
-- idempotency and query support, data class, network scope, licenses), the
-- remote revision evidence and the connection-layer identity it was read
-- through; the digest covers all of it. The discoverer never reviews its
-- own revision. Catalog commands (discover, review, disable) are idempotent
-- per (tenant, command); reviews are append-only.
--
-- A grant revision binds tenant/subject, canonical resource, issuer and
-- audience, protocol/transport, descriptor revision and digest, methods,
-- resource/prompt selectors, purpose, data class, cost cap and expiry; the
-- policy digest over them is what Control registers. PENDING until Control's
-- receipt (the registration command is kept so a retry after a crash or a
-- lost answer reuses the original identity), ACTIVE after it, REVOKING from
-- the revocation commit (MCP denies new admission at once) until Control's
-- barrier converged, then REVOKED. Grant decisions are append-only and
-- idempotent per (tenant, command).
ALTER TABLE descriptors
    ADD COLUMN server_name       text NOT NULL DEFAULT '',
    ADD COLUMN server_version    text NOT NULL DEFAULT '',
    ADD COLUMN resources         jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN prompts           jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN resources_digest  text NOT NULL DEFAULT '',
    ADD COLUMN prompts_digest    text NOT NULL DEFAULT '',
    ADD COLUMN data_class        text NOT NULL DEFAULT 'internal' CHECK (data_class IN ('public', 'internal', 'confidential', 'restricted')),
    ADD COLUMN network_scope     text[] NOT NULL DEFAULT '{}',
    ADD COLUMN licenses          text[] NOT NULL DEFAULT '{}',
    ADD COLUMN issuer            text NOT NULL DEFAULT '',
    ADD COLUMN revision_evidence text NOT NULL DEFAULT '',
    ADD COLUMN connection_ref    text NOT NULL DEFAULT '',
    ADD COLUMN discovered_by     text NOT NULL DEFAULT '',
    ADD COLUMN reviewer          text,
    ADD COLUMN review_id         text;
-- A decision recorded before reviews were bound to the revision (no
-- reviewer, no review id) is not a review: such a revision is disabled and
-- must be rediscovered and reviewed.
UPDATE descriptors SET state = 'disabled' WHERE state IN ('approved', 'rejected');
ALTER TABLE descriptors
    ALTER COLUMN data_class DROP DEFAULT,
    ALTER COLUMN revision_evidence DROP DEFAULT,
    ALTER COLUMN discovered_by DROP DEFAULT,
    ADD CONSTRAINT descriptors_reviewed_check CHECK (state NOT IN ('approved', 'rejected') OR (reviewer IS NOT NULL AND review_id IS NOT NULL)),
    ADD CONSTRAINT descriptors_four_eyes_check CHECK (reviewer IS NULL OR reviewer <> discovered_by);

CREATE TABLE catalog_commands (
    tenant_id      text NOT NULL,
    command_id     text NOT NULL,
    command_kind   text NOT NULL CHECK (command_kind IN ('discover', 'review', 'disable')),
    request_digest text NOT NULL CHECK (request_digest ~ '^sha256:[0-9a-f]{64}$'),
    server_id      text NOT NULL,
    revision       bigint NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, command_id),
    FOREIGN KEY (server_id, revision) REFERENCES descriptors (server_id, revision)
);

ALTER TABLE reviews ADD COLUMN tenant_id text NOT NULL DEFAULT '';
ALTER TABLE reviews ALTER COLUMN tenant_id DROP DEFAULT;

ALTER TABLE grants
    ADD COLUMN canonical_resource      text NOT NULL DEFAULT '',
    ADD COLUMN transport               text NOT NULL DEFAULT '',
    ADD COLUMN protocol_version        text NOT NULL DEFAULT '',
    ADD COLUMN issuer                  text NOT NULL DEFAULT '',
    ADD COLUMN audience                text NOT NULL DEFAULT '',
    ADD COLUMN resource_selectors      text[] NOT NULL DEFAULT '{}',
    ADD COLUMN prompt_selectors        text[] NOT NULL DEFAULT '{}',
    ADD COLUMN data_class              text NOT NULL DEFAULT 'internal' CHECK (data_class IN ('public', 'internal', 'confidential', 'restricted')),
    ADD COLUMN policy_digest           text NOT NULL DEFAULT '',
    ADD COLUMN policy_epoch            bigint,
    ADD COLUMN failure_code            text,
    ADD COLUMN registration_command_id text NOT NULL DEFAULT '',
    ADD COLUMN revocation_command_id   text,
    ADD COLUMN fenced_at               timestamptz,
    ADD COLUMN in_flight_calls         bigint NOT NULL DEFAULT 0,
    ADD COLUMN unknown_calls           bigint NOT NULL DEFAULT 0,
    ADD COLUMN control_state           text NOT NULL DEFAULT 'none' CHECK (control_state IN ('none', 'fenced', 'converging', 'converged')),
    ADD COLUMN revoked_at              timestamptz;
-- Grants from before the barrier were never registered with Control under a
-- policy digest: every one not already terminal is revoked (restrictive),
-- under a deterministic command identity of the grant.
UPDATE grants SET policy_digest = 'sha256:' || encode(sha256(convert_to('pre-barrier:' || grant_id, 'UTF8')), 'hex'),
                  registration_command_id = 'pre-barrier:' || grant_id;
UPDATE grants SET state = 'revoked', revocation_command_id = 'pre-barrier-revoke:' || grant_id,
                  control_state = 'converged', revoked_at = now(), failure_code = 'PRE_BARRIER'
    WHERE state IN ('pending', 'active', 'revoking', 'revoked');
ALTER TABLE grants
    ALTER COLUMN canonical_resource DROP DEFAULT,
    ALTER COLUMN transport DROP DEFAULT,
    ALTER COLUMN protocol_version DROP DEFAULT,
    ALTER COLUMN audience DROP DEFAULT,
    ALTER COLUMN data_class DROP DEFAULT,
    ALTER COLUMN policy_digest DROP DEFAULT,
    ALTER COLUMN registration_command_id DROP DEFAULT,
    ADD CONSTRAINT grants_policy_digest_check CHECK (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
    ADD CONSTRAINT grants_active_check CHECK (state <> 'active' OR control_receipt_id IS NOT NULL),
    ADD CONSTRAINT grants_revoking_check CHECK (state NOT IN ('revoking', 'revoked') OR revocation_command_id IS NOT NULL),
    ADD CONSTRAINT grants_revoked_check CHECK (state <> 'revoked' OR (revoked_at IS NOT NULL AND control_state = 'converged'));
CREATE INDEX grants_open_barrier_idx ON grants (updated_at) WHERE state IN ('pending', 'revoking');

-- A grant revision keeps its revision through create, activate, revoke and
-- converge (the revision is what Control registered), so decisions are
-- unique per (tenant, command, decision) instead of per target revision.
ALTER TABLE grant_decisions
    ADD COLUMN tenant_id text NOT NULL DEFAULT '',
    DROP CONSTRAINT grant_decisions_grant_id_to_revision_key,
    DROP CONSTRAINT grant_decisions_decision_check,
    ADD CONSTRAINT grant_decisions_decision_check CHECK (decision IN ('create', 'activate', 'registration_failed', 'revoke', 'converge', 'expire'));
ALTER TABLE grant_decisions
    ALTER COLUMN tenant_id DROP DEFAULT,
    ADD CONSTRAINT grant_decisions_command UNIQUE (tenant_id, command_id, decision);

GRANT SELECT, INSERT ON catalog_commands TO anvilkit_mcp_app;
-- Reviews and grant decisions are append-only for the runtime role.
REVOKE UPDATE ON reviews, grant_decisions FROM anvilkit_mcp_app;

-- +goose Down
GRANT UPDATE ON reviews, grant_decisions TO anvilkit_mcp_app;
REVOKE ALL ON catalog_commands FROM anvilkit_mcp_app;
ALTER TABLE grant_decisions
    DROP CONSTRAINT grant_decisions_command,
    ADD CONSTRAINT grant_decisions_grant_id_to_revision_key UNIQUE (grant_id, to_revision),
    DROP COLUMN tenant_id;
DROP INDEX grants_open_barrier_idx;
ALTER TABLE grants
    DROP CONSTRAINT grants_revoked_check,
    DROP CONSTRAINT grants_revoking_check,
    DROP CONSTRAINT grants_active_check,
    DROP CONSTRAINT grants_policy_digest_check,
    DROP COLUMN revoked_at,
    DROP COLUMN control_state,
    DROP COLUMN unknown_calls,
    DROP COLUMN in_flight_calls,
    DROP COLUMN fenced_at,
    DROP COLUMN revocation_command_id,
    DROP COLUMN registration_command_id,
    DROP COLUMN failure_code,
    DROP COLUMN policy_epoch,
    DROP COLUMN policy_digest,
    DROP COLUMN data_class,
    DROP COLUMN prompt_selectors,
    DROP COLUMN resource_selectors,
    DROP COLUMN audience,
    DROP COLUMN issuer,
    DROP COLUMN protocol_version,
    DROP COLUMN transport,
    DROP COLUMN canonical_resource;
ALTER TABLE reviews DROP COLUMN tenant_id;
DROP TABLE catalog_commands;
ALTER TABLE descriptors
    DROP CONSTRAINT descriptors_four_eyes_check,
    DROP CONSTRAINT descriptors_reviewed_check,
    DROP COLUMN review_id,
    DROP COLUMN reviewer,
    DROP COLUMN discovered_by,
    DROP COLUMN connection_ref,
    DROP COLUMN revision_evidence,
    DROP COLUMN issuer,
    DROP COLUMN licenses,
    DROP COLUMN network_scope,
    DROP COLUMN data_class,
    DROP COLUMN prompts_digest,
    DROP COLUMN resources_digest,
    DROP COLUMN prompts,
    DROP COLUMN resources,
    DROP COLUMN server_version,
    DROP COLUMN server_name;
