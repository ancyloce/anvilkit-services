# profile-schemas

The shared, language-neutral part of the configuration lifecycle (DD-09 §4/§5, delivery.md P14-06): two JSON Schemas (2020-12) and their cross-language fixtures. No configuration service, no cross-language framework: each service keeps its own loader (koanf in Go, the explicit loader in TypeScript, pydantic/jsonschema in Python) and agrees with these documents.

- `apollo-snapshot.schema.json` — the reviewed export of one Apollo release that a service consumes as its central non-secret configuration during normal operation or an Apollo outage: app, cluster, namespace, release key, fetched/expires instants, dotted configuration keys with string values; secret and placement keys are excluded by name; an expired or invalid snapshot never starts a generation.
- `config-generation.schema.json` — what a service records for every generation it publishes: service, generation number, digest of the merged non-secret inputs, secret revision (a digest, never a value), Apollo release, profile digests.
- `fixtures.json` — the shared positive/negative cases (`now` is the instant the snapshot cases are evaluated at).
- `python/config_generation.py` — the executable Python consumer for the Inference service (P15 integrates it): `python3 python/config_generation.py --fixtures fixtures.json` runs the cases; with `--config`/`--snapshot` and `ANVILKIT_INFERENCE_*` it builds and prints a generation record.

Consumers of the fixtures: `services/agent/mcp` (`internal/config`, Go), `services/agent/knowledge` (`src/config.ts`, TypeScript), this package's Python example; `tools/run-verification.py` runs the Python cases in its `background` step.
