# AnvilKit patch of the CloudPirates valkey chart 0.25.11

Upstream: `oci://registry-1.docker.io/cloudpirates/valkey` 0.25.11 (Apache-2.0, see `LICENSE`).

Change 1, in `templates/statefulset.yaml`: the Sentinel container's start script no longer
runs `cat /tmp/sentinel.conf`. That file holds `sentinel auth-pass <master> <password>` and
`requirepass <password>`, so upstream writes the queue password into the container log, where
`deploy/qualification/check-telemetry.py` found it (P23-06). The chart version carries the
suffix `-anvilkit.1`; the chart name stays `valkey` so the StatefulSet selectors are unchanged.
Drop this copy once upstream stops printing the configuration.

Change 2 (P0.6), in `templates/statefulset.yaml` and `templates/sentinel-master-proxy-service.yaml`:
upstream refuses `tls.enabled` together with `sentinel.masterProxy` because the HAProxy health
checks speak plaintext RESP to the servers. The proxy stays `mode tcp` (the clients' TLS passes
through to the servers' TLS port end to end; HAProxy holds no server certificate) and its checks
now run over TLS (`check-ssl verify required ca-file … check-sni <server FQDN>` against the
mounted CA of the TLS Secret); the master-proxy Service renders with TLS too. The chart version
carries the suffix `-anvilkit.2`.

Change 3 (P0.6), in `templates/_helpers.tpl`, `templates/statefulset.yaml` and
`templates/prestop-configmap.yaml`: upstream's Sentinel never speaks TLS (its container mounts no
certificate and its configuration names no TLS file), so with `tls.enabled` it waits forever for
the TLS-only server and the replication group never forms. With `tls.enabled` the Sentinel now
listens on TLS only (`port 0`, `tls-port` = `sentinel.port`) and reaches the servers and the other
Sentinels over TLS (`tls-replication yes`) with the server certificate of the TLS Secret, mounted in
the Sentinel and init containers; every `valkey-cli` call to a Sentinel (init container, Sentinel
start script, readiness probe, preStop scripts) passes the TLS options (`valkey.sentinelTlsArgs`).
The chart version carries the suffix `-anvilkit.3`.

Change 4 (P0.6), in `templates/statefulset.yaml`: Change 2's health checks still went out in
plaintext. A `tcp-check connect` rule without options ignores the server line's `check-ssl`, so every
check reached the servers' TLS port as plain RESP (the servers logged `packet length too long`), all
servers read DOWN and the master endpoint refused its clients. With `tls.enabled` the rule is
`tcp-check connect default`, which takes the server line's `check-ssl`, `verify required ca-file …`
and `check-sni`. The chart version carries the suffix `-anvilkit.4`.

Change 5, in `templates/statefulset.yaml`: the init container started the server configuration
with `touch` and only appended to it, but `/tmp/valkey` is an emptyDir, which a node restart keeps
along with the Pod. Every restart therefore appended to the previous file, including the `# Generated
by CONFIG REWRITE` section where Sentinel had written `replicaof <another server>`. A server the
script makes a replica appends its own `replicaof`, which wins as the last one. The server it makes
master appends none, so it started as a replica of a server that was itself a replica. After every
host reboot of the qualification environment (2026-10-20 and 2026-10-21, three times), no server was
master and every Sentinel failover aborted (`-failover-abort-no-good-slave`), until an operator ran
`REPLICAOF NO ONE`. The script now empties the file first (`: >`) and writes the whole configuration
from the Sentinels' current view, as it already did on a fresh Pod. The chart version carries the
suffix `-anvilkit.5`.
