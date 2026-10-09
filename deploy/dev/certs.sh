#!/bin/sh
# Issues the DEVELOPMENT_ONLY workload certificates of the host side (P0.1):
# the cert-manager PKI of deploy/dev/k8s/pki.yaml (SelfSigned bootstrap ->
# anvilkit-dev-root-ca -> ClusterIssuer anvilkit-dev-ca) issues one leaf per
# principal into anvilkit-dev-pki, and this script copies each leaf's
# certificate, private key and the public CA bundle to
# .local/dev/certs/<principal>/{tls.crt,tls.key,ca.crt} (keys 0600). The CA
# private key stays in the cluster Secret; no process receives it. Re-runs
# only re-copy; the CA and unexpired leaves are not regenerated. Idempotent.
#
#   sh deploy/dev/certs.sh            # issue and synchronize
#   sh deploy/dev/certs.sh verify     # openssl chain check of every leaf
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
LOCAL="$ROOT/.local/dev"
CERTS="$LOCAL/certs"
CTX=kind-anvilkit-dev
NS=anvilkit-dev-pki
TRUST_DOMAIN=${ANVILKIT_DEV_TRUST_DOMAIN:-anvilkit.local}
export PATH="$PATH:$(go env GOPATH)/bin:$ROOT/.local/bin"
export KUBECONFIG=${ANVILKIT_DEV_ADMIN_KUBECONFIG:-$HOME/.kube/config}
k() { kubectl --context "$CTX" "$@"; }

# principal directory | identity namespace | service account | DNS names | URI SAN override
# The negatives are trusted-CA certificates the integration harness uses for
# the SEC-01 refusals: a foreign trust domain and a certificate without a URI SAN.
PRINCIPALS='
anvilkit-agent-api|anvilkit-apps|anvilkit-agent-api||
anvilkit-agent-control|anvilkit-apps|anvilkit-agent-control||
anvilkit-agent-workflow|anvilkit-apps|anvilkit-agent-workflow||
anvilkit-agent-model-proxy|anvilkit-apps|anvilkit-agent-model-proxy||
anvilkit-agent-knowledge|anvilkit-apps|anvilkit-agent-knowledge||
anvilkit-agent-mcp|anvilkit-apps|anvilkit-agent-mcp||
anvilkit-agent-background-worker|anvilkit-apps|anvilkit-agent-background-worker||
anvilkit-agent-inference|anvilkit-apps|anvilkit-agent-inference||
anvilkit-job-access-sidecar|anvilkit-components|anvilkit-job-access-sidecar||
negative-foreign-trust-domain|anvilkit-apps|anvilkit-agent-api||spiffe://other.invalid/ns/anvilkit-apps/sa/anvilkit-agent-api
negative-no-uri|anvilkit-apps|anvilkit-agent-api||none
'

case "${1:-issue}" in
  issue)
    k apply -f "$ROOT/deploy/dev/k8s/pki.yaml" >/dev/null
    k -n cert-manager wait certificate/anvilkit-dev-root-ca --for=condition=Ready --timeout=120s >/dev/null
    k wait clusterissuer/anvilkit-dev-ca --for=condition=Ready --timeout=60s >/dev/null
    echo "$PRINCIPALS" | while IFS='|' read -r dir idns sa dns uri; do
      [ -n "$dir" ] || continue
      name="$sa"
      case "$uri" in
        "") uris="    - spiffe://$TRUST_DOMAIN/ns/$idns/sa/$sa" ;;
        none) uris="" ;;
        *) uris="    - $uri" ;;
      esac
      k apply -f - >/dev/null <<YAML
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: $dir
  namespace: $NS
spec:
  commonName: $name
  secretName: $dir
  duration: 720h
  renewBefore: 240h
  privateKey:
    algorithm: ECDSA
    size: 256
    rotationPolicy: Always
  usages: [digital signature, key encipherment, server auth, client auth]
  dnsNames:
    - $name
    - $name.$idns.svc
    - $name.$idns.svc.cluster.local
    - localhost
  ipAddresses:
    - 127.0.0.1
$( [ -n "$uris" ] && printf '  uris:\n%s\n' "$uris" )
  issuerRef:
    name: anvilkit-dev-ca
    kind: ClusterIssuer
    group: cert-manager.io
YAML
    done
    echo "$PRINCIPALS" | while IFS='|' read -r dir idns sa dns uri; do
      [ -n "$dir" ] || continue
      k -n "$NS" wait "certificate/$dir" --for=condition=Ready --timeout=120s >/dev/null
      mkdir -p "$CERTS/$dir"
      chmod 700 "$CERTS/$dir"
      umask 077
      k -n "$NS" get secret "$dir" -o go-template='{{index .data "tls.crt"}}' | base64 -d > "$CERTS/$dir/tls.crt"
      k -n "$NS" get secret "$dir" -o go-template='{{index .data "tls.key"}}' | base64 -d > "$CERTS/$dir/tls.key"
      k -n "$NS" get secret "$dir" -o go-template='{{index .data "ca.crt"}}' | base64 -d > "$CERTS/$dir/ca.crt"
      chmod 600 "$CERTS/$dir/tls.key"
      chmod 644 "$CERTS/$dir/tls.crt" "$CERTS/$dir/ca.crt"
    done
    # The public CA bundle alone, for clients that only verify.
    k -n cert-manager get secret anvilkit-dev-root-ca -o go-template='{{index .data "tls.crt"}}' | base64 -d > "$CERTS/ca.crt"
    chmod 644 "$CERTS/ca.crt"
    echo "development certificates synchronized under $CERTS (trust domain $TRUST_DOMAIN)"
    ;;
  verify)
    rc=0
    for d in "$CERTS"/*/; do
      n=$(basename "$d")
      if openssl verify -CAfile "$CERTS/ca.crt" "$d/tls.crt" >/dev/null 2>&1; then
        san=$(openssl x509 -in "$d/tls.crt" -noout -ext subjectAltName | tr -d '\n' | sed 's/^ *X509v3 Subject Alternative Name: *//;s/  */ /g')
        echo "PASS $n: $san"
      else
        echo "FAIL $n: does not chain to $CERTS/ca.crt"; rc=1
      fi
    done
    exit $rc
    ;;
  *)
    echo "usage: $0 [issue|verify]" >&2; exit 2 ;;
esac
