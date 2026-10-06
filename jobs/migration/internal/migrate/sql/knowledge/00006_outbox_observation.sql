-- +goose Up
-- The runtime's outbox observation (the anvilkit_knowledge_outbox_oldest_
-- unforwarded_seconds gauge: the age of the oldest message the forwarder has
-- not acknowledged) reads the forwarder's acknowledged position. The
-- forwarder identity owns outbox_offsets (00002); the app role only needs to
-- read it, which 00002 did not grant, so every observation failed with
-- "permission denied for table outbox_offsets" and the gauge never moved
-- (found by the P23 qualification). Read only: the offsets stay the
-- forwarder's to write.
GRANT SELECT ON outbox_offsets TO anvilkit_knowledge_app;

-- +goose Down
REVOKE SELECT ON outbox_offsets FROM anvilkit_knowledge_app;
