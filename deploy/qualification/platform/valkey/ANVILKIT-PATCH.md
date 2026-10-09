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
