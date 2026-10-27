#!/bin/sh
# The Jobs node of the Compose placement (plan 0006 option B, owner decision
# D-2): one RKE2 node on the Compose host that runs only the Job namespaces;
# the services stay in Compose. Run on the server from the deployed
# configuration (~/anvilkit, the repository layout), as the deploy user with
# sudo, in this order:
#
#   sh deploy/compose/jobs/jobs-node.sh host           # anvilkit-jobs0 (10.255.255.1) and the inbound guard
#   sh deploy/compose/jobs/jobs-node.sh guard-confirm  # from a NEW ssh connection: keep and enable the guard
#   sh deploy/compose/jobs/jobs-node.sh install        # RKE2 and Helm, pinned and checksum-verified
#   sh deploy/compose/jobs/jobs-node.sh cluster        # Kyverno, the admission chart, Job namespaces, relay, policies
#   sh deploy/compose/jobs/jobs-node.sh identities     # launcher identities and kubeconfigs, sidecar identity
#   sh deploy/compose/jobs/jobs-node.sh probe          # the path probe (probe.yaml)
#   sh deploy/compose/jobs/jobs-node.sh status
#
# Every step is idempotent. Downloads are verified against the pins below before
# use; nothing is built. The node token, the launcher tokens and the sidecar key
# go to restricted files or straight into the cluster and are never printed.
# `identities` again after pki.sh renews the sidecar leaf, or before the
# launcher tokens expire (one year); then recreate the Workflow and Knowledge.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
export ANVILKIT_HOME=${ANVILKIT_HOME:-/srv/anvilkit}
SECRETS="$ANVILKIT_HOME/secrets"
CERTS="$ANVILKIT_HOME/certs"
RKE2_VERSION='v1.36.4+rke2r1'
RKE2_INSTALL_SHA256=42983c86d1da64a92061d83afb57630cedd69241989f1b0673f3db6c3d92ee6b
HELM_VERSION=v3.19.0
HELM_SHA256=a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496
KYVERNO_VERSION=v1.19.1
KYVERNO_SHA256=d3322cb346d3d42dd0f41e230b0d1d7bc5619960e1c36fdac4d9151d724b88e6
ADDRESS=10.255.255.1
PUBLIC_IF=eth0
ADMIN=/etc/rancher/rke2/rke2.yaml
K="sudo /var/lib/rancher/rke2/bin/kubectl --kubeconfig $ADMIN"
H="sudo /usr/local/bin/helm --kubeconfig $ADMIN"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

need_host() {
  ip -4 addr show dev anvilkit-jobs0 2>/dev/null | grep -q "inet $ADDRESS/32" || { echo "anvilkit-jobs0 has no $ADDRESS: run host first" >&2; exit 1; }
  sudo nft list table inet anvilkit_guard >/dev/null 2>&1 || { echo "the inbound guard is not loaded: run host first" >&2; exit 1; }
}

host() {
  [ "$(ip -4 route show default | awk '{print $5; exit}')" = "$PUBLIC_IF" ] || { echo "the default route does not leave through $PUBLIC_IF (guard.nft names it)" >&2; exit 1; }
  systemctl is-active --quiet systemd-networkd || { echo "systemd-networkd is not active (anvilkit-jobs0 needs it)" >&2; exit 1; }
  sudo install -m 0644 "$HERE/anvilkit-jobs0.netdev" "$HERE/anvilkit-jobs0.network" /etc/systemd/network/
  sudo networkctl reload
  i=0
  until ip -4 addr show dev anvilkit-jobs0 2>/dev/null | grep -q "inet $ADDRESS/32"; do
    i=$((i + 1)); [ "$i" -lt 30 ] || { echo "anvilkit-jobs0 did not get $ADDRESS" >&2; exit 1; }; sleep 1
  done
  echo "anvilkit-jobs0 up with $ADDRESS"
  sudo install -d -m 0755 /etc/anvilkit
  sudo install -m 0644 "$HERE/guard.nft" /etc/anvilkit/guard.nft
  sudo install -m 0644 "$HERE/anvilkit-guard.service" /etc/systemd/system/anvilkit-guard.service
  sudo systemctl daemon-reload
  if ! systemctl is-enabled --quiet anvilkit-guard.service; then
    # First application: the guard removes itself in three minutes unless
    # guard-confirm, run over a new SSH connection, keeps it.
    sudo systemd-run --quiet --unit anvilkit-guard-rollback --on-active=180 /usr/sbin/nft delete table inet anvilkit_guard
    echo "guard armed with a 3-minute rollback: run guard-confirm from a new ssh connection"
  fi
  sudo nft -f /etc/anvilkit/guard.nft
  echo "guard loaded on $PUBLIC_IF"
}

guard_confirm() {
  sudo systemctl stop anvilkit-guard-rollback.timer 2>/dev/null || true
  sudo systemctl enable --now anvilkit-guard.service >/dev/null
  sudo nft list table inet anvilkit_guard >/dev/null
  echo "guard confirmed and enabled at boot"
}

install_node() {
  need_host
  if [ "$(/usr/local/bin/rke2 --version 2>/dev/null | awk 'NR==1 {print $3}')" != "$RKE2_VERSION" ]; then
    curl -fsSL -o "$WORK/install.sh" "https://raw.githubusercontent.com/rancher/rke2/$(echo "$RKE2_VERSION" | sed 's/+/%2B/')/install.sh"
    echo "$RKE2_INSTALL_SHA256  $WORK/install.sh" | sha256sum -c --quiet
    # The script downloads the release tarball and verifies it against the release's checksum file.
    sudo env INSTALL_RKE2_VERSION="$RKE2_VERSION" INSTALL_RKE2_TYPE=server INSTALL_RKE2_METHOD=tar sh "$WORK/install.sh"
  fi
  sudo install -d -m 0700 /etc/rancher/rke2
  changed=0
  sudo cmp -s "$HERE/rke2-config.yaml" /etc/rancher/rke2/config.yaml || changed=1
  sudo install -m 0600 "$HERE/rke2-config.yaml" /etc/rancher/rke2/config.yaml
  sudo systemctl enable rke2-server.service >/dev/null 2>&1
  if [ "$changed" = 1 ] || ! systemctl is-active --quiet rke2-server.service; then
    sudo systemctl restart rke2-server.service
  fi
  i=0
  until $K get node anvilkit-jobs >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -lt 120 ] || { echo "node anvilkit-jobs did not register" >&2; exit 1; }; sleep 5
  done
  $K wait --for=condition=Ready node/anvilkit-jobs --timeout=600s >/dev/null
  echo "RKE2 $RKE2_VERSION: node anvilkit-jobs Ready"
  if [ "$(/usr/local/bin/helm version --template '{{.Version}}' 2>/dev/null)" != "$HELM_VERSION" ]; then
    curl -fsSL -o "$WORK/helm.tgz" "https://get.helm.sh/helm-$HELM_VERSION-linux-amd64.tar.gz"
    echo "$HELM_SHA256  $WORK/helm.tgz" | sha256sum -c --quiet
    tar -xzf "$WORK/helm.tgz" -C "$WORK" linux-amd64/helm
    sudo install -m 0755 "$WORK/linux-amd64/helm" /usr/local/bin/helm
  fi
  echo "Helm $HELM_VERSION"
}

cluster() {
  need_host
  curl -fsSL -o "$WORK/kyverno.yaml" "https://github.com/kyverno/kyverno/releases/download/$KYVERNO_VERSION/install.yaml"
  echo "$KYVERNO_SHA256  $WORK/kyverno.yaml" | sha256sum -c --quiet
  # --force-conflicts: the scale below owns the admission controller's replicas.
  $K apply --server-side --force-conflicts -f "$WORK/kyverno.yaml" >/dev/null
  $K -n kyverno rollout status deploy/kyverno-admission-controller --timeout=300s >/dev/null
  $K -n kyverno scale deploy/kyverno-admission-controller --replicas=3 >/dev/null
  $K apply -f - >/dev/null <<'PDB'
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: kyverno-admission-controller, namespace: kyverno}
spec:
  minAvailable: 2
  selector: {matchLabels: {app.kubernetes.io/component: admission-controller, app.kubernetes.io/instance: kyverno}}
PDB
  $K -n kyverno rollout status deploy/kyverno-admission-controller --timeout=300s >/dev/null
  echo "Kyverno $KYVERNO_VERSION: admission controller 3 replicas"
  $K apply -f "$HERE/cluster.yaml" >/dev/null
  $K apply -f "$ROOT/deploy/policies/network/anvilkit-components-egress.yaml" -f "$ROOT/deploy/policies/network/anvilkit-parsing-egress.yaml" >/dev/null
  $H upgrade --install anvilkit-job-admission "$ROOT/deploy/policies/chart" -n kyverno \
    -f "$HERE/admission-values.yaml" --wait --timeout 5m >/dev/null
  $K -n kube-system rollout status ds/anvilkit-seccomp-installer --timeout=180s >/dev/null
  $K -n anvilkit-gateway rollout status deploy/anvilkit-compose-gateway --timeout=180s >/dev/null
  echo "admission chart, seccomp profile, Job namespaces, relay and network policies in place"
}

identities() {
  need_host
  # The launchers' least-privilege identities, rendered from the charts that own them.
  $H template anvilkit-agent-workflow-compose "$ROOT/services/agent/workflow/deploy/chart" -n anvilkit-components \
    --set temporal.address=unused --set control.address=unused --set launcher.backend=rke2-anvilkit-jobs \
    --set launcher.imageRegistry=unused --set launcher.sidecarControlAddress=unused \
    --set development.enabled=true --set temporal.tls.mode=development \
    --set identity.trustDomain=anvilkit.local --set identity.certificate.issuerRef.name=unused \
    -s templates/serviceaccount.yaml -s templates/rbac.yaml | $K -n anvilkit-components apply -f - >/dev/null
  $H template anvilkit-agent-knowledge-compose "$ROOT/services/agent/knowledge/deploy/chart" -n anvilkit-parsing \
    --set parser.enabled=true --set serviceAccount.name=anvilkit-agent-knowledge-compose --set database.secret.name=unused --set nats.url=unused \
    --set forwarder.database.secret.name=unused --set relay.database.secret.name=unused --set relay.queue.secret.name=unused \
    --set development.enabled=true --set nats.tls.mode=development --set identity.certificate.issuerRef.name=unused \
    -s templates/serviceaccount.yaml -s templates/rbac.yaml | $K -n anvilkit-parsing apply -f - >/dev/null
  # The access sidecar's workload identity: the leaf pki.sh issued, as the
  # Secret the Workflow's launcher mounts into the sidecar container only.
  leaf="$CERTS/anvilkit-job-access-sidecar"
  sudo test -s "$leaf/tls.key" || { echo "no sidecar leaf under $leaf (pki.sh issue)" >&2; exit 1; }
  $K -n anvilkit-components create secret generic anvilkit-job-access-sidecar-identity --type=kubernetes.io/tls \
    --from-file=tls.crt="$leaf/tls.crt" --from-file=tls.key="$leaf/tls.key" --from-file=ca.crt="$leaf/ca.crt" \
    --dry-run=client -o yaml | $K apply -f - >/dev/null
  ca=$(sudo sed -n 's/^ *certificate-authority-data: //p' "$ADMIN" | head -1)
  for spec in "launcher.kubeconfig anvilkit-components anvilkit-agent-workflow-compose" \
              "knowledge-launcher.kubeconfig anvilkit-parsing anvilkit-agent-knowledge-compose"; do
    set -- $spec
    token=$($K -n "$2" create token "$3" --duration 8760h)
    (umask 077 && cat > "$SECRETS/$1.new" <<KUBE
apiVersion: v1
kind: Config
clusters:
  - name: anvilkit-jobs
    cluster:
      server: https://anvilkit-jobs-api:6443
      certificate-authority-data: ${ca}
users:
  - name: $3
    user:
      token: ${token}
contexts:
  - name: anvilkit-jobs
    context: {cluster: anvilkit-jobs, user: $3, namespace: $2}
current-context: anvilkit-jobs
KUBE
)
    chmod 644 "$SECRETS/$1.new" && mv "$SECRETS/$1.new" "$SECRETS/$1"
    echo "$1: $3 in $2 (token valid one year)"
  done
  echo "sidecar identity Secret in anvilkit-components; recreate the Workflow and Knowledge to read the kubeconfigs"
}

probe() {
  $K -n anvilkit-probe delete job anvilkit-jobs-path-probe --ignore-not-found >/dev/null 2>&1 || true
  $K apply -f "$HERE/probe.yaml" >/dev/null
  i=0
  until [ -n "$($K -n anvilkit-probe get job anvilkit-jobs-path-probe -o jsonpath='{.status.succeeded}{.status.failed}')" ]; do
    i=$((i + 1)); [ "$i" -lt 40 ] || break; sleep 5
  done
  $K -n anvilkit-probe logs job/anvilkit-jobs-path-probe
  $K -n anvilkit-probe logs job/anvilkit-jobs-path-probe | grep -qx ANVILKIT_JOBS_PATH_PROBE_PASS
}

status() {
  echo "## node"; $K get nodes -o wide
  echo "## rke2: $(/usr/local/bin/rke2 --version | head -1)"
  echo "## guard drop rules: $(sudo nft list table inet anvilkit_guard | grep -c drop)"
  echo "## kyverno"; $K -n kyverno get deploy
  echo "## admission policies"; $K get validatingpolicies -o name
  echo "## Job namespaces"; $K get resourcequota,networkpolicy -n anvilkit-components; $K get resourcequota,networkpolicy -n anvilkit-parsing
  echo "## relay"; $K -n anvilkit-gateway get deploy,svc
}

case "${1:-}" in
  host) host ;;
  guard-confirm) guard_confirm ;;
  install) install_node ;;
  cluster) cluster ;;
  identities) identities ;;
  probe) probe ;;
  status) status ;;
  *) echo "usage: $0 host|guard-confirm|install|cluster|identities|probe|status" >&2; exit 2 ;;
esac
