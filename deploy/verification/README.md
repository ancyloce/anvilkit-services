# Local repair verification environment

This environment belongs to the F01–F12 repair run. It is separate from the everyday `anvilkit-dev`, legacy `anvilkit-local`, and existing kind application deployments. The local secret platform runs in kind; the three-VM application verification environment runs RKE2. Neither substitutes for full platform qualification.

## Actual access and ownership

Paths are relative to the repository unless absolute. These paths were created and used on 2026-09-18; they contain local credentials and are ignored by Git.

| Item | Actual location |
| --- | --- |
| RKE2 kubeconfig | `.local/repair-rke2/kubeconfig.yaml` (mode 0600) |
| RKE2 context | `anvilkit-rke2-verification` |
| SSH inventory | `.local/repair-rke2/ssh-inventory.json` |
| Windows SSH key and known hosts | `D:\AnvilKit\verification\access\id_ed25519`, `known_hosts`; private key ACL limited to the current Windows user |
| VM disks / ownership marker | `D:\AnvilKit\verification\{application,data,sandbox}\`, `D:\AnvilKit\verification\owner.json` |
| Registry | `https://10.203.0.12:30500` (TLS, authentication, persistent storage) |
| Registry settings / CA / scoped Docker config | `.local/repair-rke2/registry/settings.json`, `ca.crt`, `docker/config.json` |
| Registry credentials | `.local/repair-rke2/registry/auth.json` (0600; do not print) |
| Node registry configuration | `/etc/rancher/rke2/registries.yaml` (0600), `/etc/rancher/rke2/registry-ca.crt` on each VM |
| Dependency credentials | `.local/repair-rke2/dependencies.json` (0600; do not print) |
| OpenBao context / namespace | `kind-anvilkit-dev` / `anvilkit-secrets` |
| OpenBao TLS / initialization material | `.local/openbao/` (0700; private files 0600; do not print `init.json` or private keys) |

| VM | Address | Allocation | Placement |
| --- | --- | --- | --- |
| `anvilkit-verify-application` | `10.203.0.11` | 2 vCPU, 6 GiB static RAM, 40 GiB disk | RKE2 server and business services |
| `anvilkit-verify-data` | `10.203.0.12` | 2 vCPU, 6 GiB static RAM, 40 GiB disk | PostgreSQL, Valkey, NATS, MinIO, Temporal development server, registry |
| `anvilkit-verify-sandbox` | `10.203.0.13` | 2 vCPU, 4 GiB static RAM, 40 GiB disk | tainted sandbox node, runsc RuntimeClass |

The owned Hyper-V internal switch and WinNAT are named `AnvilKit Verification`, subnet `10.203.0.0/24`. Existing switches and VMs were preserved. Ubuntu 24.04.5, RKE2 `v1.36.4+rke2r1`, containerd `2.3.4-k3s1.36`, and gVisor `release-20260914.0` were used. The Ubuntu source SHA-256 is `612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354` (Noble cloud image, 20260911).

## Setup and verification commands

Use `.local/verification-venv/bin/python` for these scripts, `.local/bin/kubectl`, and `.local/bin/helm`. Provisioning requires Windows Administrator access, Hyper-V, Windows OpenSSH, `qemu-img`, and `cloud-localds`; these capabilities were available and used. Do not rerun initial VM creation on existing VMs: it deliberately refuses to overwrite them.

The setup sequence is `provision-hyperv.py`, `configure-hyperv-network.py`, `configure-rke2.py`, `install-gvisor.py`, `install-registry.py`, then `install-services.py`. Initial provisioning reads the verified Ubuntu image and checksum file from `.local/repair-rke2/`. Windows SSH uses the protected copy of the generated key in the access directory above. Image build evidence and the resolved pushed digests are in `outputs/repairs-f01-f12/image-*.log`, `push-*.log`, and `images.json`; `values/*.yaml` pins each installed business image. The helper Dockerfiles under `images/` combine existing owner, forwarder, and Worker builds without adding a second business container. `supervise.mjs` starts the trusted processes, probes all of them, and bounds group shutdown to 20 seconds.

`install-services.py` installs only into the owned namespace. The dependencies use exclusively owned databases, queues, buckets, streams, and VM storage. PostgreSQL app/migrator/relay/forwarder roles are separate. Temporal here is the existing development test server with persistent storage, not an HA deployment. The post-renderer sets one replica, Recreate, no HPA or injected sidecars; the actual Pod checks also reject init and ephemeral containers. Inference has no implementation and is not scaffolded.

```sh
.local/verification-venv/bin/python deploy/verification/check-deployments.py
.local/verification-venv/bin/python deploy/verification/verify-background.py
.local/verification-venv/bin/python deploy/verification/verify-proxy-store.py
node --test deploy/verification/images/supervise.test.mjs
```

For deployed API tests, start these explicit IPv4 forwards in separate terminals, then run the test wrapper. A bind error must be resolved before running tests; a listener on an occupied port must never be assumed to belong to this environment.

```sh
.local/bin/kubectl --kubeconfig .local/repair-rke2/kubeconfig.yaml -n anvilkit-verification port-forward --address 127.0.0.1 svc/anvilkit-agent-api 29100:80
.local/bin/kubectl --kubeconfig .local/repair-rke2/kubeconfig.yaml -n anvilkit-verification port-forward --address 127.0.0.1 svc/postgres 25434:5432
.local/verification-venv/bin/python deploy/verification/run-deployed-tests.py
```

Disposable cross-service tests can run from the host with the real RKE2 client configuration. They create their own PostgreSQL/Temporal/MinIO or PostgreSQL/Valkey/NATS containers, and do not use the everyday environment's data:

```sh
KUBECONFIG="$PWD/.local/repair-rke2/kubeconfig.yaml" \
ANVILKIT_INTEGRATION_JOB_NAMESPACE=anvilkit-verification-sandbox \
ANVILKIT_INTEGRATION_LAUNCH_BACKEND=anvilkit-rke2-verification \
go test -C tests/integration -tags integration \
  -run '^(TestModelProxyControlledCalls|TestPreparationAndGenerationLifecycle|TestBackgroundLane)$' -count=1 -v ./...
```

Heavy Job concurrency is one through the sandbox Job quota, Generation capacity is one, and the background Worker concurrency is one. The local DeepSeek verification route uses dated reviewed prices and a tenant-scoped DEVELOPMENT_ONLY authorization. Provider verification runs one model send at a time and retains task allocations and every per-send admission; there is no daily spending cap. The gVisor probe passed token absence and denied egress checks, but the full candidate observer/UID/socket/seccomp qualification has not run. Candidate profiles remain disabled.

Cleanup must verify the resource's ownership label, recorded UID, or Testcontainers ownership before deleting it. There is no blanket cleanup command. No shared `FLUSHALL` path remains in the active integration tests.

## OpenBao and credential entry

OpenBao 2.6.2 (chart 0.29.5) runs with TLS and a persistent 2 GiB Raft PVC. CSI provider 2.0.3 and Secrets Store CSI driver 1.6.1 are separate platform components; all their Linux images are digest pinned. The injector and Kubernetes Secret synchronization are disabled. This is a single-member local installation with protected local unseal material, not production seal custody or disaster-recovery qualification.

```sh
.local/verification-venv/bin/python deploy/verification/openbao/install.py
.local/bin/kubectl --context kind-anvilkit-dev -n anvilkit-secrets port-forward --address 127.0.0.1 svc/openbao 18200:8200
# In another terminal, after the TLS forward is listening:
.local/verification-venv/bin/python deploy/verification/openbao/bootstrap.py
.local/verification-venv/bin/python deploy/verification/openbao/verify-auth.py
.local/verification-venv/bin/python deploy/verification/openbao/verify-csi.py
```

The read policy covers only KV v2 mount `kv`, path `anvilkit/local/model-proxy/deepseek`, field `api_key`. Kubernetes authentication binds the `anvilkit-agent-model-proxy` ServiceAccount in `anvilkit-verification`, with audience `openbao`. Other workload identities and paths are denied. The separate short-lived interactive-entry token can write this path only. The CSI file is read-only and is mounted only for the Model Proxy identity; no business sidecar or copied Kubernetes credential Secret is required. See the [official provider parameters](https://openbao.org/docs/platform/k8s/csi/configurations/).

The DeepSeek credential and its user-entered account/project association were verified present on 2026-09-19 without displaying either. For entry or rotation, run the following command directly in a local interactive terminal. It asks for the actual account/project association and uses hidden input for the key, verified TLS, and a one-use write token. It does not put the key in argv, shell history, source, images, or output. No account/project identifier has been invented.

```sh
.local/verification-venv/bin/python deploy/verification/openbao/enter-deepseek.py
```

RKE2 now uses the separate `kubernetes-rke2` OpenBao auth mount. Its dedicated cross-cluster reviewer has only TokenReview/create permission; its revocable local service-account token stays in platform storage, never a business Pod. The workload role binds only Model Proxy's ServiceAccount, namespace and `openbao` audience. The driver and provider run as platform DaemonSets on the application node. The real Model Proxy Pod mounts the credential file read-only through CSI; no credential is copied into a Kubernetes Secret. File mode 0444 permits the nonroot service to read its private CSI volume. Other business identities and unrelated secret paths were denied in runtime checks.

The existing kind OpenBao serves RKE2 through a TLS-preserving port-forward on `10.203.0.1:18200`. The provider trusts the existing CA and verifies `openbao.anvilkit-secrets.svc`; no insecure TLS mode is used. The persistent WSL systemd unit is [anvilkit-verification-openbao-forward.service](openbao/anvilkit-verification-openbao-forward.service). The Windows Hyper-V inbound rule permits this port only from the three verification VM addresses. This local tunnel and single-member Bao remain development infrastructure, not production availability or custody qualification.

## Restore the existing installation

Inspect actual Hyper-V state and verify `D:\AnvilKit\verification\owner.json`, VM Notes and disk/network paths before starting only the recorded existing VMs. The 2026-09-19 inspection found all three VMs **Off**; starting those VMs restored the API, registry and cluster dependencies without replacing disks or cluster state. Failed SSH banners alone were not used as proof of VM state.

For OpenBao restoration, preserve its PVC and protected material. Do not rerun installation or initialization just because the service is sealed. Start the localhost TLS forward above, then:

```sh
.local/verification-venv/bin/python deploy/verification/openbao/restore-existing.py
systemctl restart anvilkit-verification-openbao-forward.service
# Only when restoring the existing cross-cluster configuration:
.local/verification-venv/bin/python deploy/verification/openbao/connect-rke2.py
.local/verification-venv/bin/python deploy/verification/openbao/verify-rke2.py
```

`restore-existing.py` refuses an uninitialized installation. `connect-rke2.py` installs no OpenBao server, checks resource ownership and renders/strictly dry-runs its changes. The negative auth and CSI test creates a unique non-provider fixture and removes only its own resources in `finally`.

## Real-provider evidence and limits

`verify-deepseek.py` executed two serial Preparation tasks on 2026-09-19 through the deployed API, Workflow, Control and Model Proxy. One asked for clarification and was canceled; the other froze a brief. Both had one successful admitted send, one native usage observation, one cost entry, no active permit afterward and no duplicate send or charge on intake replay. The total ledger cost was USD 0.001266. This is usage-based local metering, not an independently audited provider invoice.

The immutable price revision `deepseek-flash-offpeak-2026-09-19` uses the [published DeepSeek prices](https://api-docs.deepseek.com/quick_start/pricing): USD 0.15 / 0.003 / 0.60 per million cache-miss input / cached input / output tokens. Its interval ends at **2026-09-21 00:00 UTC**; refresh the reviewed observation before another window. Control's inclusion flags interpret cached and reasoning counters as subsets of native totals; the native observations remain unchanged. Existing price revisions retain their independent-category behavior. Control rounds upward per category to micro-USD.

```sh
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  .local/verification-venv/bin/python deploy/verification/verify-deepseek.py
```

The script creates temporary local budget grants, funds each task through normal Control transactions, and expires only its own grants in `finally`. Paid observations and costs are retained. If a run reports UNKNOWN or exceeds its wait, inspect and reconcile the recorded original operation/call identities; do not rerun it as a replacement. No daily cap or automatic provider retry is added.

Evidence for this follow-up is in `outputs/repairs-f05-f12-followup/`. Complete candidate generation, real Pagix/Studio interfaces, and the remaining qualification gates are still unexecuted. Preparation, LocalCheck, controlled upstreams and Temporal tests do not establish those missing results.
