# Unresolved effect age

**Alerts.** `AnvilKitUnresolvedObligationAgeHigh` (an `authorized`/`unknown` dispatch or a `permitted`/`unknown` external effect older than the threshold), `AnvilKitModelProxyUnknownOutcomes` (model calls ended UNKNOWN other than SENDER_LOST), `AnvilKitMCPUnknownCalls` (an MCP tool call UNKNOWN beyond the threshold).

**Dashboards.** `anvilkit-control` (Unresolved obligations, oldest), `anvilkit-model-proxy` (Call outcomes, UNKNOWN outcomes), `anvilkit-mcp` (UNKNOWN calls, oldest UNKNOWN call).

**What it means.** A physical send or business write may have happened and its outcome is not known (DD-02 (architecture `execution.md`)). The operation that owns it stays `reconciling`; its costs and reservations are not settled. The only legal resolution is evidence about the **original** identity: a provider/upstream receipt, a signed not-sent attestation, or an operator disposition with evidence. A timeout never proves that nothing happened.

**Diagnose.**

```sh
# Which class and state, and how many (the gauges' source query, read-only):
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_control -Atc \
  "SELECT 'dispatch', state, count(*), min(admitted_at) FROM dispatches WHERE state IN ('authorized','unknown') GROUP BY state
   UNION ALL SELECT 'effect', state, count(*), min(created_at) FROM effect_intents WHERE state IN ('permitted','unknown') GROUP BY state"
# The reconciler's view (Control logs, no identifiers beyond the dispatch id):
$K -n anvilkit-apps logs deploy/anvilkit-agent-control -c control --since=1h | grep -E 'reconcil|outcome query'
# Model Proxy: the calls that ended unknown and why (callId, dispatchId, outcome, errorCode only):
$K -n anvilkit-apps logs deploy/anvilkit-agent-model-proxy -c model-proxy --since=1h | grep -E '"outcome":"unknown"|PERMISSION_LOST'
# MCP: the UNKNOWN calls (tenant, server, age):
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_mcp -Atc \
  "SELECT tenant_id, server_id, call_id, now() - updated_at FROM tool_requests WHERE state = 'unknown' ORDER BY updated_at LIMIT 50"
```

**Remedy.**

1. Let the periodic reconcilers run first: Control's dispatch reconciler queries the Model Proxy under the original dispatch identity every `dispatch.reconcile_interval` (minimum age `dispatch.reconcile_age`), MCP's call reconciler asks Control every `calls.reconcile_interval`. A provider or upstream receipt arriving later settles the obligation without operator action.
2. For an MCP call whose upstream outcome the operator established out of band, reconcile under its original identity (needs the `call-operator` role in the call's tenant; never sends upstream):
   `curl -sS -X POST -H "Authorization: Bearer $OPERATOR_TOKEN" https://<api>/api/v1/mcp/calls/<callId>/reconciliations`
3. For a dispatch or effect that no query can resolve, a disposition with evidence is recorded inside a recovery run (`POST /api/v1/recoveries/<runId>/dispositions`, API-17, needs the `operator` role; see [recovery-epoch.md](recovery-epoch.md)); without evidence the obligation stays UNKNOWN and the operation stays `reconciling`. No bulk UNKNOWN-to-success.

**Verify.** The gauge `anvilkit_control_unresolved_obligation_oldest_seconds{class,state}` (or `anvilkit_mcp_tool_call_unknown_oldest_seconds`) falls below the threshold; the operation reaches a terminal lifecycle through `GET /api/v1/operations/<operationId>`; no second send appears for the call identity (`anvilkit_model_proxy_requests_total` and the upstream's own receipts).

**Owner.** Control (dispatch/effects), Model Proxy, MCP; finance for any cost the disposition settles.
