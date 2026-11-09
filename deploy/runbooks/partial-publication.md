# Partial publication

**Alerts.** `AnvilKitReleasePartiallyPublished`: a release has stayed `partially_published` longer than the `partialPublicationSeconds` threshold (Control's `anvilkit_control_release_oldest_seconds{state}`; `anvilkit_control_releases_in_flight{state}` counts the in-flight releases by state). An UNKNOWN publication or activation target raises `AnvilKitUnresolvedObligationAgeHigh` (class `effect`; [unresolved-effect-age.md](unresolved-effect-age.md)), which points here when the effect belongs to a release.

**Dashboards.** `anvilkit-control` (unresolved obligations by class and state), `anvilkit-workflow`.

**What it means.** A release publishes one exact ReleaseSubject to two targets, npm and browser/CSS, and activates only after both receipts (DD-06 §4 (architecture `components.md`)). A partial publication means one target succeeded and the other failed or is UNKNOWN. A successful target is never resent automatically, and an npm-only release is never activated.

**Diagnose.**

```sh
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_control -Atc \
  "SELECT operation_id, tenant_id, state, subject_digest, npm, browser, activation, failure_code, updated_at FROM releases WHERE state IN ('partially_published', 'reconciling') ORDER BY updated_at"
$K -n anvilkit-data exec "$PRIMARY" -c postgres -- psql -d anvilkit_control -Atc \
  "SELECT effect_id, operation_id, kind, occurrence, state, inventory_state, updated_at FROM effect_intents WHERE kind IN ('publication', 'activation') AND state IN ('sent', 'unknown') ORDER BY updated_at"
curl -sS -H "Authorization: Bearer $OPERATOR_TOKEN" https://<api>/api/v1/operations/<operationId>/release
```

**Remedy.**

1. Freeze the release: its subject digest and the receipts it holds stand; no new release of the same subject and no activation run until the targets are known.
2. An UNKNOWN target is queried under its original effect identity: the Release workflow asks the Pagix adapter about the original identity on its next round. Without upstream query support, the effect stays UNKNOWN for controlled reconciliation; it is never published again under a new effect ID.
3. A target that failed definitively: a continuation or compensation is the Pagix owner's decision. It uses the original identity, the exact subject and a current admission, never a generic retry.

**Verify.** The release leaves `partially_published` for `published` and then `activated`, or ends `failed` with its recorded receipts; no publication or activation effect stays `unknown` past its bound.

**Owner.** The Pagix adapter (Workflow) and the Pagix integration owner; Control holds the record.
