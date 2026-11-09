# MCP grant revocation or SSRF refusal

**Alerts.** `AnvilKitMCPRevocationSlow`: the p95 from a grant revocation to its senders' convergence exceeds the threshold. Related: `AnvilKitMCPUnknownCalls` and `AnvilKitMCPRecoveryRestricted` ([mcp-restriction.md](mcp-restriction.md)). `AnvilKitMCPEgressRefused`: MCP's egress refused at least one upstream hop in the last 15 minutes, by `reason` (`anvilkit_mcp_egress_refusals_total`: `malformed`, `host_not_allowed`, `unresolved`, `address`, `peer_mismatch`, `scheme`, `userinfo`, `redirect`, `out_of_scope`). The counter covers discovery, the authorization hops (metadata, token and JWKS endpoints) and tool calls. A refused call ends with the failure code `EGRESS_REFUSED`, a refused authorization hop with `AUTHORIZATION_SUBSTITUTED`; the label never names a host, tenant or server.

**Dashboards.** `anvilkit-mcp` (grant transitions, revocation latency by stage, tool call transitions, UNKNOWN calls).

**What it means.** A revocation blocks new admission at once (the Control barrier) and then waits for the grant's senders to converge (DD-08 (architecture `mcp.md`)). A slow convergence means calls admitted before the revocation are still in flight or UNKNOWN. Admission closure is not an external cancellation: a call already sent may still take effect upstream. An `EGRESS_REFUSED` call was stopped before any byte left MCP. It may mean a misconfigured upstream, or a tool result or descriptor that steers MCP toward an internal address.

**Diagnose.**

```sh
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_mcp -Atc \
  "SELECT grant_id, tenant_id, state, revision, updated_at FROM grants WHERE state = 'revoking' ORDER BY updated_at"
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_mcp -Atc \
  "SELECT tenant_id, server_id, call_id, state, failure_code, updated_at FROM tool_requests WHERE failure_code = 'EGRESS_REFUSED' OR state IN ('sent', 'unknown') ORDER BY updated_at DESC LIMIT 50"
curl -sS -H "Authorization: Bearer $OPERATOR_TOKEN" https://<api>/api/v1/mcp/grants/<grantId>/revocation
```

The last call reads the revocation's progress: blocked admission versus converged senders (API-16).

**Remedy.**

1. A suspected SSRF: revoke the grants of the server involved (`POST /api/v1/mcp/grants/<grantId>/revocations`, API-16), so admission closes and the Control barrier starts. Keep the refused calls as evidence, and review the server's descriptor and recent tool results.
2. Record every call that was already sent: each `sent` or `unknown` call is reconciled under its original identity ([mcp-restriction.md](mcp-restriction.md), [unresolved-effect-age.md](unresolved-effect-age.md)); never resend one.
3. A slow convergence without SSRF: find the senders still holding admitted calls (MCP replicas, the Model Proxy for tool round trips) and let them finish or reach UNKNOWN; do not cut them off with a forced restart ([proxy-fleet-restart.md](proxy-fleet-restart.md)).

**Verify.** The revocation's progress shows every sender converged; the grant is `revoked`; no new call to the server is admitted; `AnvilKitMCPRevocationSlow` clears.

**Owner.** MCP; Security for an SSRF review.
