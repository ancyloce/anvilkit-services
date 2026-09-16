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
echo "object stores ready: anvilkit-artifacts (versioned), anvilkit-inventory (own user)"
