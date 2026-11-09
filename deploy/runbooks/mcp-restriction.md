# MCP recovery restriction

**Alert.** `AnvilKitMCPRecoveryRestricted`: one or more (tenant, server) pairs refuse new tool calls with `RECOVERY_RESTRICTED` because a call to that server has an UNKNOWN outcome.

**Dashboards.** `anvilkit-mcp` (UNKNOWN calls and restricted servers, oldest UNKNOWN call, RECOVERY_RESTRICTED refusals, Tool call transitions).

**What it means.** An uncertain tool send restricts its server for its tenant (DD-08 (architecture `mcp.md`)): a new call could duplicate an effect the first one already caused. The restriction lifts by itself once the UNKNOWN call is resolved under its original identity; it is never lifted by hand.

**Diagnose.**

```sh
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_mcp -Atc \
  "SELECT tenant_id, server_id, call_id, control_dispatch_id, now() - updated_at FROM tool_requests WHERE state = 'unknown' ORDER BY updated_at"
$K -n anvilkit-apps logs deploy/anvilkit-agent-mcp -c mcp --since=6h | grep -E 'outcome unknown|in-flight send is recorded UNKNOWN'
```

A call recorded UNKNOWN at shutdown (`shutdown: an in-flight send is recorded UNKNOWN`) points at a rollout that cut a send: follow [proxy-fleet-restart.md](proxy-fleet-restart.md)'s grace check for MCP (`calls.send_timeout` below `terminationGracePeriodSeconds`).

**Remedy.**

1. Establish the upstream's outcome for the call out of band (the upstream's own request log or receipt for the call's idempotency key).
2. Record it where Control decides the dispatch (the dispatch's not-sent evidence or outcome; see [unresolved-effect-age.md](unresolved-effect-age.md)); then reconcile the call under its original identity (call-operator role in the tenant; sends nothing upstream):
   `curl -sS -X POST -H "Authorization: Bearer $OPERATOR_TOKEN" https://<api>/api/v1/mcp/calls/<callId>/reconciliations`
3. The reconciler also asks Control every `calls.reconcile_interval`; repeat step 2 only after Control's record changed.

**Verify.** `anvilkit_mcp_recovery_restricted_servers` drops to 0 for the pair; a new call to the server is admitted; the reconciled call shows its resolved state in `GET /api/v1/mcp/calls/<callId>`.

**Owner.** MCP; the tenant's call operator for the out-of-band evidence.
