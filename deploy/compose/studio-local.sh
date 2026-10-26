#!/bin/sh
# The Studio from a local checkout against the Compose placement
# (DEVELOPMENT_ONLY; owner decision 2026-10-25: the Studio is not deployed as a
# Compose service, it is tested from the local environment).
#
#   up      SSH tunnel to the server (local 9100 -> API, local 9000 -> MinIO,
#           because signed upload URLs name minio:9000), the `minio` alias in
#           /etc/hosts, a 0600 copy of the tenant_a fixture API token outside
#           any repository, and the Studio dev server with the two server-side
#           variables (ANVILKIT_AGENT_API_URL, ANVILKIT_AGENT_API_TOKEN).
#   down    stops the dev server and the tunnel, removes the alias line this
#           script added and the token copy.
#   status  tunnel, API readiness through it, alias, Studio.
#
# Open http://localhost:3000/agent/... (localhost, not 127.0.0.1: Next.js
# blocks its dev resources for other hosts). Nothing is installed or built:
# the Studio checkout must already have its dependencies. The browser never
# sees the token or the API address; the Studio server relays every call.
#
# Environment: ANVILKIT_SERVER (ubuntu@43.135.132.23), ANVILKIT_STUDIO_DIR
# (anvilkit-studio next to this repository), ANVILKIT_STUDIO_STATE
# (${XDG_STATE_HOME:-$HOME/.local/state}/anvilkit-studio-local).
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
SERVER=${ANVILKIT_SERVER:-ubuntu@43.135.132.23}
STUDIO=${ANVILKIT_STUDIO_DIR:-$(cd "$HERE/../../.." && pwd)/anvilkit-studio}
STATE=${ANVILKIT_STUDIO_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/anvilkit-studio-local}
SOCK="$STATE/tunnel.sock"
TOKEN="$STATE/api-token"
PIDFILE="$STATE/studio.pgid"
LOG="$STATE/studio-dev.log"
MARK="# anvilkit studio-local (deploy/compose/studio-local.sh)"

tunnel_up() {
  if ssh -S "$SOCK" -O check "$SERVER" 2>/dev/null; then echo "tunnel: already open"; return; fi
  ssh -f -N -M -S "$SOCK" -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
    -L 127.0.0.1:9100:127.0.0.1:9100 -L 127.0.0.1:9000:127.0.0.1:29000 "$SERVER"
  echo "tunnel: open (127.0.0.1:9100 -> API, 127.0.0.1:9000 -> MinIO)"
}

alias_up() {
  if getent ahosts minio 2>/dev/null | awk '{print $1}' | grep -qx 127.0.0.1; then echo "alias: minio -> 127.0.0.1"; return; fi
  if [ -w /etc/hosts ]; then
    printf '127.0.0.1 minio %s\n' "$MARK" >> /etc/hosts
    echo "alias: added 127.0.0.1 minio to /etc/hosts"
  else
    echo "alias: MISSING; add '127.0.0.1 minio' to /etc/hosts (root) or uploads fail" >&2
  fi
}

token_up() {
  (umask 077; ssh -o BatchMode=yes "$SERVER" "python3 -c \"import json; d=json.load(open('/srv/anvilkit/secrets/api-principals.json')); print(next(k for k, v in d.items() if v['tenantId'] == 'tenant_a'), end='')\"" > "$TOKEN.new")
  [ -s "$TOKEN.new" ] || { rm -f "$TOKEN.new"; echo "token: could not read the fixture principal on $SERVER" >&2; exit 1; }
  mv "$TOKEN.new" "$TOKEN"
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "Authorization: Bearer $(cat "$TOKEN")" http://127.0.0.1:9100/api/v1/knowledge/sources || true)
  [ "$code" = 200 ] && echo "token: tenant_a, accepted by the API" || { echo "token: API answered $code" >&2; exit 1; }
}

# The dev server runs in a session of its own (setsid); its id is in $PIDFILE.
session_pids() { [ -f "$PIDFILE" ] && ps -eo pid=,sid= | awk -v s="$(cat "$PIDFILE")" '$2 == s {print $1}'; }
listeners() { ss -ltnpH 'sport = :3000' 2>/dev/null; ss -ltnpH 'sport = :31234' 2>/dev/null; }
studio_running() { [ -n "$(session_pids)" ]; }

studio_up() {
  if studio_running; then echo "studio: already running (session $(cat "$PIDFILE"))"; return; fi
  [ -z "$(listeners)" ] || { echo "studio: port 3000 or 31234 is taken by another process:" >&2; listeners >&2; exit 1; }
  [ -d "$STUDIO/apps/studio/node_modules" ] || { echo "studio: no dependencies in $STUDIO (install them there first)" >&2; exit 1; }
  : > "$LOG"
  # Its own session, so `down` finds every process it started; the dev
  # supervisor (scripts/dev-collab.mjs) stops next-server and the collab relay,
  # which it runs in groups of their own, on SIGTERM.
  ANVILKIT_AGENT_API_URL=http://127.0.0.1:9100 ANVILKIT_AGENT_API_TOKEN="$(cat "$TOKEN")" \
  ANVILKIT_PAGE_STORAGE=memory NEXT_TELEMETRY_DISABLED=1 \
    setsid sh -c "cd '$STUDIO/apps/studio' && exec pnpm dev" > "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  i=0
  while [ $i -lt 180 ]; do
    grep -q 'Ready in' "$LOG" && break
    studio_running || { tail -20 "$LOG" >&2; echo "studio: exited during start" >&2; exit 1; }
    sleep 1; i=$((i + 1))
  done
  grep -q 'Ready in' "$LOG" || { tail -20 "$LOG" >&2; echo "studio: not ready after 180 s (log $LOG)" >&2; exit 1; }
  echo "studio: ready at http://localhost:3000/agent/knowledge (log $LOG)"
}

down() {
  if studio_running; then
    # Plain PIDs: dash's kill takes no negative group id.
    kill -s TERM $(session_pids) 2>/dev/null || true
    i=0; while { studio_running || [ -n "$(listeners)" ]; } && [ $i -lt 20 ]; do sleep 1; i=$((i + 1)); done
    if studio_running; then kill -s KILL $(session_pids) 2>/dev/null || true; fi
    if [ -n "$(listeners)" ]; then
      echo "studio: still listening after 20 s (not killed; stop these yourself):" >&2; listeners >&2
    else
      echo "studio: stopped"
    fi
  fi
  rm -f "$PIDFILE"
  ssh -S "$SOCK" -O exit "$SERVER" 2>/dev/null && echo "tunnel: closed" || true
  if [ -w /etc/hosts ] && grep -qF "$MARK" /etc/hosts; then
    grep -vF "$MARK" /etc/hosts > "$STATE/hosts.tmp" && cat "$STATE/hosts.tmp" > /etc/hosts && rm -f "$STATE/hosts.tmp"
    echo "alias: removed"
  fi
  rm -f "$TOKEN"
}

status() {
  ssh -S "$SOCK" -O check "$SERVER" 2>/dev/null && echo "tunnel: open" || echo "tunnel: closed"
  echo "API readyz through the tunnel: $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:9100/readyz || true)"
  echo "alias minio: $(getent ahosts minio 2>/dev/null | awk 'NR==1 {print $1}')"
  studio_running && echo "studio: running (session $(cat "$PIDFILE")), http $(curl -s -o /dev/null -w '%{http_code}' --max-time 30 http://localhost:3000/agent/knowledge || true)" || echo "studio: stopped"
}

mkdir -p "$STATE" && chmod 700 "$STATE"
case "${1:-status}" in
  up) tunnel_up; alias_up; token_up; studio_up ;;
  down) down ;;
  status) status ;;
  *) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
