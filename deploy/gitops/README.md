# GitOps and the deployment lock (P23)

The parent owns each environment's pinned combination: which chart at which version, which image digest and which environment values every component gets, and in which order Argo CD synchronizes them ([release pipeline](../../docs/architecture/platform.md#operations), step 5). Service charts stay in their service repositories.

| Path | Purpose |
| --- | --- |
| `lock.yaml` | Deployment lock and capacity worksheet: replica, autoscaling, disruption, placement and connection-pool limits of all eight services and every DD-10 platform component, per environment. Production fields are `REQUIRED` until ENV-01..05/09 exist; `tools/check-deployment-lock.py --self-test` (the `deployment` step of `tools/run-verification.py`) checks the qualification environment against it. |
| `environment/` | The `anvilkit-environment` chart: one Argo CD `AppProject` and one `Application` per component, ordered by sync wave (platform dependencies, stateful services, migrations, applications, probes). The root Application waits for each wave to be Healthy, so a failed wave stops the later ones. |
| `environments/<environment>.yaml` | An environment's combination; references to release artifacts are resolved at publication. Only `qualification` exists (DEVELOPMENT_ONLY). |
| `publish.py` | Mirrors the pinned upstream charts, publishes the parent's platform charts and the resolved environment chart with per-run versions into the environment's registry, signs and verifies each, and writes the root Application. |

Every source Argo CD synchronizes is a versioned, signed OCI chart; nothing is read from a branch. Argo CD does not verify Cosign signatures on Helm charts itself: `publish.py` verifies them before publishing, and the cluster's admission policy verifies every application image. The registry is the development foundation's CNCF Distribution registry, not Harbor (lock: `registry` NOT_RUN).

```sh
.local/verification-venv/bin/python tools/release-artifacts.py --run RUN
.local/verification-venv/bin/python deploy/gitops/publish.py --environment qualification --run RUN
.local/verification-venv/bin/python deploy/qualification/bootstrap.py --run RUN
```
