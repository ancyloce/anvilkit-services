#!/bin/sh
# DEVELOPMENT_ONLY workload PKI of the Compose placement (P0.1 shape without
# cert-manager): one self-signed EC root CA and one leaf per principal with the
# same names as deploy/dev/certs.sh issues (DNS: <name>, <name>.<ns>.svc,
# <name>.<ns>.svc.cluster.local, localhost; IP 127.0.0.1; URI SAN
# spiffe://<trust domain>/ns/<ns>/sa/<name>), so every service's SPIFFE
# allowlist and server-name verification are unchanged. Keys never leave
# ${ANVILKIT_HOME}/certs (0700); each principal directory is 0755 with 0644 files
# because the containers' own users (65532, 10001) read them through a bind mount
# whose parent is not traversable on the host. Idempotent: an unexpired leaf is
# kept; `renew` reissues every leaf.
#
#   sh deploy/compose/pki.sh [issue|renew|verify]
set -eu
HOME_DIR=${ANVILKIT_HOME:-/srv/anvilkit}
CERTS="$HOME_DIR/certs"
TRUST_DOMAIN=${ANVILKIT_TRUST_DOMAIN:-anvilkit.local}
DAYS=${ANVILKIT_CERT_DAYS:-365}
# principal | identity namespace
PRINCIPALS='
anvilkit-agent-api|anvilkit-apps
anvilkit-agent-control|anvilkit-apps
anvilkit-agent-workflow|anvilkit-apps
anvilkit-agent-model-proxy|anvilkit-apps
anvilkit-agent-knowledge|anvilkit-apps
anvilkit-agent-mcp|anvilkit-apps
anvilkit-agent-background-worker|anvilkit-apps
anvilkit-agent-inference|anvilkit-apps
anvilkit-job-access-sidecar|anvilkit-components
'
mode=${1:-issue}
umask 022
mkdir -p "$CERTS" && chmod 700 "$CERTS"
mkdir -p "$CERTS/ca"
if [ ! -f "$CERTS/ca/ca.key" ]; then
  (umask 077; openssl ecparam -genkey -name prime256v1 -noout -out "$CERTS/ca/ca.key")
  openssl req -x509 -new -key "$CERTS/ca/ca.key" -sha256 -days 3650 -subj "/CN=anvilkit-compose-ca" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -out "$CERTS/ca/ca.crt"
  chmod 644 "$CERTS/ca/ca.crt"
  echo "root CA created ($CERTS/ca/ca.crt)"
fi
issue() {
  name=$1; ns=$2; dir="$CERTS/$name"
  mkdir -p "$dir" && chmod 755 "$dir"
  if [ "$mode" != renew ] && [ -f "$dir/tls.crt" ] && openssl x509 -in "$dir/tls.crt" -noout -checkend 2592000 >/dev/null 2>&1; then
    cp "$CERTS/ca/ca.crt" "$dir/ca.crt"; return
  fi
  (umask 077; openssl ecparam -genkey -name prime256v1 -noout -out "$dir/tls.key.new")
  openssl req -new -key "$dir/tls.key.new" -subj "/CN=$name" -out "$dir/csr.pem"
  cat > "$dir/ext.cnf" <<EXT
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyAgreement
extendedKeyUsage=serverAuth,clientAuth
subjectAltName=DNS:$name,DNS:$name.$ns.svc,DNS:$name.$ns.svc.cluster.local,DNS:localhost,IP:127.0.0.1,URI:spiffe://$TRUST_DOMAIN/ns/$ns/sa/$name
EXT
  openssl x509 -req -in "$dir/csr.pem" -CA "$CERTS/ca/ca.crt" -CAkey "$CERTS/ca/ca.key" -CAcreateserial -sha256 -days "$DAYS" \
    -extfile "$dir/ext.cnf" -out "$dir/tls.crt.new" >/dev/null 2>&1
  mv "$dir/tls.key.new" "$dir/tls.key" && mv "$dir/tls.crt.new" "$dir/tls.crt"
  cp "$CERTS/ca/ca.crt" "$dir/ca.crt"
  rm -f "$dir/csr.pem" "$dir/ext.cnf"
  # Read by the container's own user through the bind mount; the host directory
  # above is 0700.
  chmod 644 "$dir/tls.key" "$dir/tls.crt" "$dir/ca.crt"
  echo "issued $name (spiffe://$TRUST_DOMAIN/ns/$ns/sa/$name, $DAYS days)"
}
case "$mode" in
  issue|renew)
    echo "$PRINCIPALS" | while IFS='|' read -r name ns; do [ -n "$name" ] && issue "$name" "$ns"; done
    ;;
  verify)
    rc=0
    echo "$PRINCIPALS" | while IFS='|' read -r name ns; do
      [ -n "$name" ] || continue
      if openssl verify -CAfile "$CERTS/ca/ca.crt" "$CERTS/$name/tls.crt" >/dev/null 2>&1; then
        san=$(openssl x509 -in "$CERTS/$name/tls.crt" -noout -ext subjectAltName | tr -d '\n' | sed 's/^ *X509v3 Subject Alternative Name: *//;s/  */ /g')
        echo "PASS $name: $san"
      else
        echo "FAIL $name: does not chain to $CERTS/ca/ca.crt"; rc=1
      fi
    done
    exit $rc
    ;;
  *) echo "usage: $0 [issue|renew|verify]" >&2; exit 2 ;;
esac
