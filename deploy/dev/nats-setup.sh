#!/bin/sh
# Runs inside the nats-box container: creates the three domain streams of
# the event catalog (ANVILKIT_KNOWLEDGE, ANVILKIT_MCP, ANVILKIT_CONTROL;
# subjects anvilkit.<domain>.>) from the reviewed DEVELOPMENT_ONLY
# definitions under deploy/dev/nats (file storage, one replica, 7-day
# retention, 1 MiB messages, a 2-minute duplicate window). Idempotent: an
# existing stream is left as it is; the reviewed values are not production
# storage, replication or retention (ENV-05, DD-10).
set -eu
for name in knowledge mcp control; do
  upper=$(echo "$name" | tr '[:lower:]' '[:upper:]')
  if nats --server "$NATS_URL" stream info "ANVILKIT_$upper" >/dev/null 2>&1; then
    echo "stream ANVILKIT_$upper present"
  else
    nats --server "$NATS_URL" stream add "ANVILKIT_$upper" --config "/setup/nats/anvilkit-$name.json" >/dev/null
    echo "stream ANVILKIT_$upper created"
  fi
done
