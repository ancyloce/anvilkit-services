#!/bin/sh
# Prepares the object stores of the development foundation on the MinIO of
# deploy/dev/compose.yaml. Runs once per `up.sh`, idempotently, with the root
# credentials of .local/dev/minio.env (DEVELOPMENT_ONLY):
#   - anvilkit-artifacts, versioned: Control's artifact store (P08). Versioning
#     is what the artifact adapter requires: every upload gets an immutable
#     version id and a finalized transfer names exactly one of them. Control
#     reaches it with the root credentials of the foundation.
#   - anvilkit-inventory: the shared obligation inventory of Control replicas
#     in the kind cluster (DD-02 §5; the S3 adapter's conditional creates and
#     listings), reached with its own user and a policy limited to that
#     bucket (.local/dev/minio-inventory.env), so the two permission
#     boundaries stay separate as Control's configuration requires. Host-side
#     development runs keep the filesystem inventory.
#   - anvilkit-model-proxy: the Model Proxy's call records and private native
#     evidence (P11; conditional creates and replaces by ETag), reached with
#     its own user and bucket-limited policy (.local/dev/minio-model-proxy.env).
#   - anvilkit-knowledge: Knowledge's uploads, content-addressed source
#     copies and parser results (P15; create-only copies, presigned GET/PUT
#     for the parser Jobs), reached with its own user and bucket-limited
#     policy (.local/dev/minio-knowledge.env).
#   - anvilkit-memory-removals: Knowledge's removal inventory (P23; immutable
#     records of memory deletions and revocations created only when absent,
#     listed after a database restore), reached with its own user whose
#     policy lists, reads and creates in that bucket and never deletes
#     (.local/dev/minio-removals.env).
set -eu
mc alias set dev http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing dev/anvilkit-artifacts >/dev/null
mc version enable dev/anvilkit-artifacts >/dev/null
mc mb --ignore-existing dev/anvilkit-inventory >/dev/null
cat > /tmp/inventory-policy.json <<'POLICY'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": ["arn:aws:s3:::anvilkit-inventory"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": ["arn:aws:s3:::anvilkit-inventory/*"]}
  ]
}
POLICY
mc admin policy create dev anvilkit-inventory /tmp/inventory-policy.json >/dev/null
if ! mc admin user info dev "$ANVILKIT_INVENTORY_ACCESS_KEY_ID" >/dev/null 2>&1; then
  mc admin user add dev "$ANVILKIT_INVENTORY_ACCESS_KEY_ID" "$ANVILKIT_INVENTORY_SECRET_ACCESS_KEY" >/dev/null
fi
mc admin policy attach dev anvilkit-inventory --user "$ANVILKIT_INVENTORY_ACCESS_KEY_ID" >/dev/null 2>&1 || true
mc mb --ignore-existing dev/anvilkit-model-proxy >/dev/null
cat > /tmp/model-proxy-policy.json <<'POLICY'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": ["arn:aws:s3:::anvilkit-model-proxy"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": ["arn:aws:s3:::anvilkit-model-proxy/*"]}
  ]
}
POLICY
mc admin policy create dev anvilkit-model-proxy /tmp/model-proxy-policy.json >/dev/null
if ! mc admin user info dev "$ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID" >/dev/null 2>&1; then
  mc admin user add dev "$ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID" "$ANVILKIT_MODEL_PROXY_STORE_SECRET_ACCESS_KEY" >/dev/null
fi
mc admin policy attach dev anvilkit-model-proxy --user "$ANVILKIT_MODEL_PROXY_STORE_ACCESS_KEY_ID" >/dev/null 2>&1 || true
mc mb --ignore-existing dev/anvilkit-knowledge >/dev/null
cat > /tmp/knowledge-policy.json <<'POLICY'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": ["arn:aws:s3:::anvilkit-knowledge"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": ["arn:aws:s3:::anvilkit-knowledge/*"]}
  ]
}
POLICY
mc admin policy create dev anvilkit-knowledge /tmp/knowledge-policy.json >/dev/null
if ! mc admin user info dev "$ANVILKIT_KNOWLEDGE_OBJECTS_ACCESS_KEY_ID" >/dev/null 2>&1; then
  mc admin user add dev "$ANVILKIT_KNOWLEDGE_OBJECTS_ACCESS_KEY_ID" "$ANVILKIT_KNOWLEDGE_OBJECTS_SECRET_ACCESS_KEY" >/dev/null
fi
mc admin policy attach dev anvilkit-knowledge --user "$ANVILKIT_KNOWLEDGE_OBJECTS_ACCESS_KEY_ID" >/dev/null 2>&1 || true
mc mb --ignore-existing dev/anvilkit-memory-removals >/dev/null
cat > /tmp/removals-policy.json <<'POLICY'
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": ["arn:aws:s3:::anvilkit-memory-removals"]},
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"], "Resource": ["arn:aws:s3:::anvilkit-memory-removals/*"]}
  ]
}
POLICY
mc admin policy create dev anvilkit-memory-removals /tmp/removals-policy.json >/dev/null
if ! mc admin user info dev "$ANVILKIT_KNOWLEDGE_REMOVALS_ACCESS_KEY_ID" >/dev/null 2>&1; then
  mc admin user add dev "$ANVILKIT_KNOWLEDGE_REMOVALS_ACCESS_KEY_ID" "$ANVILKIT_KNOWLEDGE_REMOVALS_SECRET_ACCESS_KEY" >/dev/null
fi
mc admin policy attach dev anvilkit-memory-removals --user "$ANVILKIT_KNOWLEDGE_REMOVALS_ACCESS_KEY_ID" >/dev/null 2>&1 || true
echo "object stores ready: anvilkit-artifacts (versioned), anvilkit-inventory (own user), anvilkit-model-proxy (own user), anvilkit-knowledge (own user), anvilkit-memory-removals (own user)"
