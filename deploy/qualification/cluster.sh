#!/bin/sh
# P23 qualification cluster (DEVELOPMENT_ONLY), separate from the development
# foundation's anvilkit-dev cluster and data services, which it never touches.
#
#   sh deploy/qualification/cluster.sh up      # mirror, DR store, kind cluster, registry hosts
#   sh deploy/qualification/cluster.sh stop    # stops the six node containers (frees the host's memory)
#   sh deploy/qualification/cluster.sh start   # starts them again on their etcd peer certificates' IPs
#   sh deploy/qualification/cluster.sh down    # deletes the kind cluster anvilkit-qualification only
#   sh deploy/qualification/cluster.sh down-dr # also removes the DR store container (its data
#                                              # directory under .local/qualification/dr stays)
#
# Containers on the kind Docker network, owned by this script by exact name:
#   anvilkit-qualification-mirror[-quay|-ghcr|-k8s|-kyverno]
#                                  pull-through caches (CNCF Distribution) of Docker Hub, quay.io,
#                                  ghcr.io, registry.k8s.io and reg.kyverno.io: the six nodes pull
#                                  each image across the link once
#   anvilkit-qualification-dr      the DR object store (MinIO) outside the cluster: Control's
#                                  obligation inventory, Knowledge's memory removal inventory,
#                                  database base backups and WAL, the
#                                  Qdrant/etcd/OpenBao snapshots and the bootstrap material
#                                  copies. It shares this host, so it is not an independent
#                                  failure domain (ENV-02).
# The development registry anvilkit-dev-registry (deploy/dev/up.sh) serves the release
# images and charts; nodes reach it by name over plain HTTP through containerd's hosts
# directory. The kubeconfig is written to .local/qualification/kubeconfig. MinIO, which
# public registries no longer serve anonymously, is vendored from the host's image cache
# into anvilkit-dev-registry:5000/vendor.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
STATE="$ROOT/.local/qualification"
NAME=anvilkit-qualification
KUBECONFIG_OUT="$STATE/kubeconfig"
REGISTRY_IMAGE=registry:3@sha256:1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33
MINIO_IMAGE=minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e
export PATH="$PATH:$(go env GOPATH)/bin:$ROOT/.local/bin"
ACTION=${1:-up}

container_running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }

case "$ACTION" in
  up)
    # Six kind nodes need more inotify instances than the kernel default (128): with
    # too few the first API server never answers and kubeadm init times out.
    if [ "$(cat /proc/sys/fs/inotify/max_user_instances)" -lt 512 ]; then
      echo "fs.inotify.max_user_instances is below 512: sysctl -w fs.inotify.max_user_instances=512 (kind's multi-node requirement)" >&2
      exit 2
    fi
    mkdir -p "$STATE/dr" "$STATE/mirror"
    chmod 0700 "$STATE"
    docker network inspect kind >/dev/null 2>&1 || { echo "the kind network is missing: run deploy/dev/up.sh first" >&2; exit 2; }
    docker inspect anvilkit-dev-registry >/dev/null 2>&1 || { echo "anvilkit-dev-registry is missing: run deploy/dev/up.sh first" >&2; exit 2; }
    # One pull-through cache per upstream registry (CNCF Distribution proxies one
    # remote each): the six nodes pull each image across the link once.
    for m in "anvilkit-qualification-mirror https://registry-1.docker.io mirror" \
             "anvilkit-qualification-mirror-quay https://quay.io mirror-quay" \
             "anvilkit-qualification-mirror-ghcr https://ghcr.io mirror-ghcr" \
             "anvilkit-qualification-mirror-k8s https://registry.k8s.io mirror-k8s" \
             "anvilkit-qualification-mirror-kyverno https://reg.kyverno.io mirror-kyverno"; do
      set -- $m
      mkdir -p "$STATE/$3"
      if ! docker inspect "$1" >/dev/null 2>&1; then
        docker run -d --restart=always --name "$1" --network kind -e REGISTRY_PROXY_REMOTEURL="$2" \
          -v "$STATE/$3:/var/lib/registry" "$REGISTRY_IMAGE" >/dev/null
      fi
      container_running "$1" || docker start "$1" >/dev/null
    done
    if [ ! -f "$STATE/dr.env" ]; then
      umask 077
      printf 'MINIO_ROOT_USER=dr-admin\nMINIO_ROOT_PASSWORD=%s\n' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > "$STATE/dr.env"
    fi
    if ! docker inspect anvilkit-qualification-dr >/dev/null 2>&1; then
      docker run -d --restart=always --name anvilkit-qualification-dr --network kind -p 127.0.0.1:29190:9000 \
        --env-file "$STATE/dr.env" -v "$STATE/dr:/data" "$MINIO_IMAGE" server /data >/dev/null
    fi
    container_running anvilkit-qualification-dr || docker start anvilkit-qualification-dr >/dev/null
    # Docker Hub and quay.io refuse MinIO to anonymous pulls: the in-cluster object
    # store runs the host-cached images, vendored into the environment registry by
    # their source digests (the push is the linux/amd64 manifest the chart pins).
    for v in "minio/minio@sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e RELEASE.2025-09-07T16-13-09Z" \
             "minio/mc@sha256:a7fe349ef4bd8521fb8497f55c6042871b2ae640607cf99d9bede5e9bdf11727 a7fe349ef4bd"; do
      set -- $v
      docker image inspect "$1" >/dev/null 2>&1 || { echo "$1 is not in the local image cache: pull it on a machine that has it and docker load it" >&2; exit 2; }
      docker tag "$1" "localhost:5001/vendor/${1%@*}:$2"
      docker push -q "localhost:5001/vendor/${1%@*}:$2" >/dev/null
    done
    if ! kind get clusters 2>/dev/null | grep -qx "$NAME"; then
      kind create cluster --config "$ROOT/deploy/qualification/kind.yaml" --kubeconfig "$KUBECONFIG_OUT" --wait 300s
    else
      kind get kubeconfig --name "$NAME" > "$KUBECONFIG_OUT"
    fi
    chmod 0600 "$KUBECONFIG_OUT"
    for node in $(kind get nodes --name "$NAME"); do
      docker exec "$node" sh -c '
        mkdir -p "/etc/containerd/certs.d/anvilkit-dev-registry:5000"
        printf "server = \"http://anvilkit-dev-registry:5000\"\n\n[host.\"http://anvilkit-dev-registry:5000\"]\n  capabilities = [\"pull\", \"resolve\"]\n" > "/etc/containerd/certs.d/anvilkit-dev-registry:5000/hosts.toml"
        for m in "docker.io https://registry-1.docker.io anvilkit-qualification-mirror" "quay.io https://quay.io anvilkit-qualification-mirror-quay" \
                 "ghcr.io https://ghcr.io anvilkit-qualification-mirror-ghcr" "registry.k8s.io https://registry.k8s.io anvilkit-qualification-mirror-k8s" \
                 "reg.kyverno.io https://reg.kyverno.io anvilkit-qualification-mirror-kyverno"; do
          set -- $m
          mkdir -p "/etc/containerd/certs.d/$1"
          printf "server = \"%s\"\n\n[host.\"http://%s:5000\"]\n  capabilities = [\"pull\", \"resolve\"]\n" "$2" "$3" > "/etc/containerd/certs.d/$1/hosts.toml"
        done'
    done
    kubectl --kubeconfig "$KUBECONFIG_OUT" get nodes -L topology.kubernetes.io/zone
    ;;
  stop)
    for node in $(kind get nodes --name "$NAME"); do docker stop -t 20 "$node" >/dev/null; done
    ;;
  start)
    # Docker assigns addresses again when stopped nodes start, while every control
    # plane's etcd peer certificate names its original address: start each control
    # plane on that address (read from the stopped container), then the workers.
    nodes=$(kind get nodes --name "$NAME")
    tmp=$(mktemp -d)
    for node in $nodes; do docker network disconnect kind "$node" 2>/dev/null || true; done
    for node in $nodes; do
      case "$node" in *control-plane*)
        docker cp "$node:/etc/kubernetes/pki/etcd/peer.crt" "$tmp/$node.crt" >/dev/null
        ip=$(openssl x509 -in "$tmp/$node.crt" -noout -ext subjectAltName | grep -oE 'IP Address:[0-9]+\.[0-9.]+' | grep -v 127.0.0.1 | head -1 | cut -d: -f2)
        docker network connect --ip "$ip" kind "$node"
        docker start "$node" >/dev/null ;;
      esac
    done
    for node in $nodes; do
      case "$node" in *control-plane*) ;; *) docker network connect kind "$node"; docker start "$node" >/dev/null ;; esac
    done
    rm -rf "$tmp"
    kubectl --kubeconfig "$KUBECONFIG_OUT" wait --for=condition=Ready nodes --all --timeout=600s
    echo "run deploy/qualification/bootstrap.py --run RUN: restarted OpenBao members are sealed"
    ;;
  down|down-dr)
    if kind get clusters 2>/dev/null | grep -qx "$NAME"; then
      kind delete cluster --name "$NAME"
    fi
    if [ "$ACTION" = down-dr ]; then
      docker rm -f anvilkit-qualification-dr anvilkit-qualification-mirror anvilkit-qualification-mirror-quay \
        anvilkit-qualification-mirror-ghcr anvilkit-qualification-mirror-k8s anvilkit-qualification-mirror-kyverno >/dev/null 2>&1 || true
    fi
    ;;
  *)
    echo "usage: $0 [up|stop|start|down|down-dr]" >&2
    exit 2
    ;;
esac
