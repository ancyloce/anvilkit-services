-- P2.7: the cleanup identity (anvilkit_knowledge_cleanup). The app role
-- never deletes; this role deletes only delivery and content rows: the
-- chunk rows of a deleted source's erasure (the purge), and the retention
-- of acknowledged outbox rows, old inbox entries, the attempts of terminal
-- requests and the launches of terminal parses. It reads what those
-- predicates need and writes nothing else.

-- +goose Up
GRANT USAGE ON SCHEMA public TO anvilkit_knowledge_cleanup;
GRANT SELECT, DELETE ON chunks, outbox, inbox, task_attempts, parse_launches TO anvilkit_knowledge_cleanup;
GRANT SELECT ON sources, ingest_requests, background_requests, outbox_offsets TO anvilkit_knowledge_cleanup;
CREATE INDEX inbox_accepted_idx ON inbox (accepted_at);

-- +goose Down
DROP INDEX inbox_accepted_idx;
REVOKE ALL ON chunks, outbox, inbox, task_attempts, parse_launches, sources, ingest_requests, background_requests, outbox_offsets FROM anvilkit_knowledge_cleanup;
REVOKE USAGE ON SCHEMA public FROM anvilkit_knowledge_cleanup;
