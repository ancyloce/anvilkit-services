# Secret rotation

**Alert.** `AnvilKitConfigGenerationRejected` (a candidate configuration generation, built after a secret or snapshot change, failed validation, construction or its probe; the previous generation stays active). Also the planned rotation of any credential.

**Dashboards.** `anvilkit-knowledge`, `anvilkit-mcp` (configuration generation), `anvilkit-overview` (Alerts firing).

**What it means.** Credentials originate in OpenBao and reach workloads through the Secrets Store CSI driver (file mounts; synced Secrets only for the consumers that need them, `anvilkit-secrets`) with a 2-minute rotation poll. Services watch their mounted files and publish a new configuration generation only after its clients are built and probed (DD-09 §4 (architecture `platform.md`)); a rejected generation keeps the old one serving.

**Planned rotation (example: a database role).**

1. Write the new value in OpenBao and change the role's password in the same step (the bootstrap tooling does both for the qualification roles): `deploy/qualification/drills.py --run <RUN> secret-rotation` exercises this path for every role and records it.
2. Wait for the CSI poll (≤ 2 min) and the service's reload interval; watch `anvilkit_<service>_config_generation` increase and `anvilkit_<service>_config_rejections_total` stay flat.
3. Only after every consumer's generation moved, retire the old credential (drop the old password/key in the upstream system).

**Rejected generation.**

```sh
$K -n anvilkit-apps logs deploy/anvilkit-agent-mcp -c mcp --since=30m | grep -E 'candidate rejected|generation'
$K -n anvilkit-apps logs deploy/anvilkit-agent-knowledge -c knowledge --since=30m | grep -E 'candidate rejected|generation'
$K -n anvilkit-apps get secretproviderclasses
$K -n anvilkit-apps describe pod -l app.kubernetes.io/name=anvilkit-agent-mcp | grep -A5 -i 'secrets-store\|mount'
```

The log names the failing step (validation, construction, probe) without the value. Fix the value in OpenBao (a wrong password, an expired certificate); the next poll builds a new candidate. Never restart a service to "pick up" a secret: a restart with a broken credential fails readiness instead of keeping the working generation.

**Verify.** The generation gauge advanced on every replica; no `candidate rejected` after the fix; the drill record shows the rotation `PASS`.

**Owner.** Platform/Security (OpenBao custody, ENV-10); the service owner for its probe.
