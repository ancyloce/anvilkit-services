-- P2.6: the single-use upload capability of a document source
-- (SourceService.BeginUpload). One row per (tenant, command): the upload id,
-- the actor and the declaration it binds (digest, size, media type), its
-- object key uploads/<tenant>/<upload_id> and its expiry. RegisterSource with
-- locator upload:<upload_id> consumes it in the registration transaction:
-- consumed once (the trigger refuses any later change of a consumed row) and
-- never deleted by the app role.

-- +goose Up
CREATE TABLE source_uploads (
    upload_id      text PRIMARY KEY CHECK (upload_id ~ '^upl-[0-9a-f]{32}$'),
    tenant_id      text NOT NULL CHECK (tenant_id <> ''),
    actor_id       text NOT NULL CHECK (actor_id <> ''),
    command_id     text NOT NULL CHECK (command_id <> ''),
    request_digest text NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
    size_bytes     bigint NOT NULL CHECK (size_bytes >= 0),
    media_type     text NOT NULL CHECK (media_type <> ''),
    object_key     text NOT NULL,
    expires_at     timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    consumed_at    timestamptz,
    source_id      text REFERENCES sources (source_id),
    UNIQUE (tenant_id, command_id),
    CONSTRAINT source_uploads_key_check CHECK (object_key = 'uploads/' || tenant_id || '/' || upload_id),
    CONSTRAINT source_uploads_consumed_check CHECK ((consumed_at IS NULL) = (source_id IS NULL))
);

-- +goose StatementBegin
CREATE FUNCTION source_uploads_once() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'upload % was already consumed', OLD.upload_id USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
-- +goose StatementEnd
CREATE TRIGGER source_uploads_once BEFORE UPDATE ON source_uploads FOR EACH ROW EXECUTE FUNCTION source_uploads_once();

GRANT SELECT, INSERT ON source_uploads TO anvilkit_knowledge_app;
GRANT UPDATE (consumed_at, source_id) ON source_uploads TO anvilkit_knowledge_app;

-- +goose Down
REVOKE ALL ON source_uploads FROM anvilkit_knowledge_app;
DROP TRIGGER source_uploads_once ON source_uploads;
DROP FUNCTION source_uploads_once();
DROP TABLE source_uploads;
