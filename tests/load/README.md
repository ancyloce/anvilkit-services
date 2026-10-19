# Load tool (P3.5)

`tests/load` drives the public API at a profile's open-model arrival rates and
reports latency distributions. It is the load of the G-14 load/capacity case
(`LOAD-CAPACITY` in `tests/evals/registry.json`), of the soak run
(`deploy/qualification/soak.py`, `LOAD-SOAK`) and of the chaos run
(`deploy/qualification/chaos.py`, `LOAD-CHAOS`).

The tool is written in Go (decided once for P3.5) and depends only on the
standard library. k6 0.49 has no Server-Sent Events client, and the SSE recovery
latency and the gap/duplicate checks of a resumed stream are part of the
measurement.

```sh
ANVILKIT_LOAD_TOKEN=... go run ./tests/load --api https://api.example \
    --profile tests/load/profiles/development.json \
    [--prometheus http://127.0.0.1:8001/api/v1/namespaces/anvilkit-observability/services/http:anvilkit-metrics-prometheus:9090/proxy] \
    [--out outputs/load/RUN] [--run RUN]
```

The bearer token is read only from `ANVILKIT_LOAD_TOKEN`. It never appears on
a command line, in the log or in `summary.json`.

## Scenarios

| Scenario | Requests | Measured |
|---|---|---|
| `local_check` | API-02 create (`local_check`, `local-check-v1`), API-04 stream, API-03, commands | `admission` (create to 202), `firstProgress` (create to the first frame past `accepted`), `endToEnd` (create to the terminal frame), `sseRecovery` (a dropped stream's reconnect with `Last-Event-ID` to its 200), `cancel`/`hold`/`resume` latency; counters for outcomes, stream gaps and duplicates, `reset_required`, dropped streams, timed-out operations, and extra listeners that missed the settlement |
| `preparation` | API-01 create with a finalized prompt transfer, API-04 | as `local_check`; settles at `waiting` (a clarification question) or a terminal lifecycle. Needs `ANVILKIT_LOAD_PROMPT_TRANSFER_ID` and `ANVILKIT_LOAD_PROMPT_DIGEST` |
| `knowledge_search` | API-08 | `search` latency, `noAnswer` count. Needs `ANVILKIT_LOAD_SNAPSHOT_ID` and `ANVILKIT_LOAD_RETRIEVAL_PROFILE_ID` |
| `mcp_catalog` | API-10 | `catalog` latency |

A scenario whose inputs are absent is `NOT_EXERCISED`, with the missing names.
It never runs with placeholders.

Model and tool dispatches are not public API calls. They happen inside the
Jobs that Generations launch, so this tool loads them only through operations
that reach those Jobs. The countable upstream's receive count is compared with
Control's admitted dispatches by `soak.py` (`--upstream-count-url`).

Arrivals are open-model: a fixed rate regardless of answers, bounded by
`maxInFlight`. An arrival over the bound is dropped and counted, never queued,
so a slow system shows as drops rather than as a hidden lower rate.

The cluster readout comes from the P3.6 Prometheus over the run's window. It
reports p50/p95/max of queue oldest waiting age, outbox oldest age, relay
backlog, Control gRPC and database pool utilization and the oldest unresolved
obligation. It reports the increase of capacity refusals, Proxy `SENDER_LOST`
outcomes and application container restarts. A query without samples is
reported as `missing`, never as zero.

## Status and exit code

| Status | Exit | Meaning |
|---|---|---|
| `MEASURED` | 0 | every scenario exercised and the cluster readout complete |
| `FAIL` | 1 | a scenario's failed-request ratio (5xx and transport errors; 4xx answers such as 429/409 are counted by code, not as failures) exceeds `maxErrorRatio` |
| `PARTIAL` | 2 | a scenario not exercised, or the cluster readout missing or incomplete |
| refused / `UNEXECUTED` | 2 | an invalid profile, or no `--api`/token |

## Profiles

`profiles/*.json` are refused on unknown fields. `classification` is either
`DEVELOPMENT_ONLY` or `ENV-05`, and an `ENV-05` profile must name its `source`
(the approved ENV-05 record). The shipped `development.json` (10 minutes) and
`soak-development.json` (4 hours, with holds) are development rates. They
measure the system under that load but size nothing. The capacity worksheet in
`docs/architecture/platform.md` stays unfilled until ENV-05 supplies the rates.

## Verification

```sh
GOWORK=off go -C tests/load vet ./... && GOWORK=off go -C tests/load test -race ./...
```

The tests run the tool against an in-process double of the API and of
Prometheus. They cover profile refusals, nearest-rank percentiles, every
latency measured, the token never in the summary, FAIL on 503s, a gap in a
resumed stream, drops at `maxInFlight`, the cancel and hold shares, and the
command's refusal and summary file. Running against the qualification
environment needs that operator's authorization. The registry gates those cases
on `ANVILKIT_QUALIFICATION_RUN`.
