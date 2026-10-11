# Local Docker Compose

This profile runs the current `local-check-v1` flow and, since development
plan S2 (2026-09-13), the fixed preparation flow: Agent API, Control and
Workflow, PostgreSQL 18.6 and Temporal Server 1.31.2. Two short-lived containers
initialize Temporal schemas and the namespace. The Control and Workflow images
build from their legacy submodules using Go 1.27.0 and their retained
contracts. The API image is pinned since 2026-09-15: `services/agent/api` now
holds the replacement API, so the legacy `anvilkit-agent-api` service uses the
image built from the old worktree (`anvilkit-local-anvilkit-agent-api:legacy-20260915`)
and rebuilds only from the preserved legacy worktree snapshot under
`.local/legacy/anvilkit-agent-api-20260915/worktree` (ignored by Git, one
machine); it never builds from the replacement.

## Start

Run from the repository root on Linux/amd64 with Docker Engine, Docker Compose,
Python 3 and OpenSSL available. The three service checkouts and parent contract
files must be present.

```sh
python3 tools/prepare-local-compose.py
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps
```

The preparation command creates ignored, private files in `.local/compose`.
It generates local caller credentials, separate API/Control and Temporal
certificate authorities, service certificates and fixture authorization.
API uses the read-only Agent database role; Control uses its write role;
Workflow receives only its Temporal connection and certificate settings.
PostgreSQL initialization applies the existing canonical role and schema files.

Host networking preserves Control's loopback-only listener without changing
service code. The following host ports must be free; all bind to loopback.

| Listener | Address |
| --- | --- |
| Public API | `127.0.0.1:18080` |
| API health (`/healthz`, `/readyz`) | `127.0.0.1:18081` |
| Control (mTLS) | `127.0.0.1:18082` |
| PostgreSQL | `127.0.0.1:15432` |
| Temporal frontend (mTLS) | `127.0.0.1:17233` |
| Temporal internal RPC | `127.0.0.1:17234–17236` |
| Temporal membership | `127.0.0.1:16933–16936` |

This stack owns its containers and named volumes under the `anvilkit-local`
Compose project. It does not reuse the earlier environment handoff.

## Submit a local check

Use the generated caller header without copying its token into a command:

```sh
curl --fail-with-body --silent --show-error \
  --header @.local/compose/caller-a.headers \
  --header 'Content-Type: application/json' \
  --data '{"commandId":"compose-plain-1","fixtureId":"plain-v1"}' \
  http://127.0.0.1:18080/v1/local-checks
```

Read the returned operation with the same header:

```sh
curl --fail-with-body --silent --show-error \
  --header @.local/compose/caller-a.headers \
  http://127.0.0.1:18080/v1/operations/OPERATION_ID
```

`plain-v1` hashes the exact 8 bytes of `AnvilKit`; `newline-v1` hashes the
9 bytes of `AnvilKit\n`. Wait for completion before submitting the second
fixture with a different `commandId`. Repeating an identical command retains
its operation identity.

## Stop, resume and inspect

```sh
docker compose logs --tail 50 anvilkit-agent-control anvilkit-agent-workflow
docker compose stop
docker compose up -d --wait
```

The `postgres-data` volume retains Agent records and Temporal history. The
`intake-data` volume retains Control's intake files and `artifact-data` its
artifact objects (preparation inputs, question sets, answer sets and briefs).
A `postgres-data` volume initialized before the S0-T04/S2 DDL lacks the
`preparation_rounds` and `preparations` relations: recreate it
(`docker compose down -v`) or apply the retained DDL delta before starting. `docker compose down`
removes the containers while retaining these volumes. Keep `.local/compose`
with the volumes: its saved credentials must match the initialized database.

Certificates and fixture authorization expire after 30 days. To refresh them,
stop the stack, rerun preparation, then recreate containers with the retained
volumes and database passwords:

```sh
docker compose stop
python3 tools/prepare-local-compose.py
docker compose up -d --force-recreate --wait
```

This profile exercises the approved local fixtures. Production orchestration,
untrusted jobs and real provider/Pagix effects retain their architecture gates.

## Submit a preparation

The same caller holds `component.prepare`. Submit a Prompt with the input
record inline (the Studio's `/agent/prepare` page does this through its
server routes), then read the task and its question set or brief:

```sh
curl --fail-with-body --silent --show-error \
  --header @.local/compose/caller-a.headers \
  --header 'Content-Type: application/json' \
  --data '{"schemaVersion":1,"commandId":"compose-prep-1","intakeSource":"ui","input":{"schemaVersion":1,"prompt":{"text":"A section announcing our spring campaign with a heading and a short description."}}}' \
  http://127.0.0.1:18080/v1/operations/preparations

curl --fail-with-body --silent --show-error \
  --header @.local/compose/caller-a.headers \
  http://127.0.0.1:18080/v1/operations/OPERATION_ID/preparation
```

Answers go to `POST /v1/operations/OPERATION_ID/preparation-answers` with the
`questionSetRef` and `operationRevision` the detail reported; the Worker
reaches Control at `https://127.0.0.1:18082` with the `anvilkit-agent-workflow`
client certificate and the retained `workflow` credential.

