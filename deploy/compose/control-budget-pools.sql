-- DEVELOPMENT_ONLY budget pools of the Compose placement (deploy/compose).
--
-- Control funds every Preparation and Generation from a current platform pool,
-- a tenant pool and an actor pool (RecordFunding; BUDGET_EXHAUSTED when one is
-- missing). The product has no pool administration route yet: Control's
-- BudgetPoolService.CreatePool is allowed only for the API's identity and the
-- API exposes no such route (README "Budget pools", open decision). Until it
-- does, this one-shot (control-budget-setup, run by `up.sh budget`) writes the
-- rows CreatePool would write, as Control's application role, for the fixture
-- principal of api-principals.json (tenant_a, user_a): one pool per level for
-- the current and the next calendar month (UTC), so a month boundary never
-- leaves the placement unfunded as long as the step runs once a month.
--
-- Idempotent: an existing pool of the same scope and period is kept as it is
-- (its cap and allocations are never changed here). Caps are scale-6 USD
-- amounts (1000000 = 1 USD) passed as psql variables by the one-shot.
\set ON_ERROR_STOP on

INSERT INTO budget_pools (pool_id, level, tenant_id, actor_id, period_start, period_end, currency, cap_amount, created_by)
SELECT 'pool_' || translate(upper(substr(md5(p.level || '/' || coalesce(p.tenant_id, '') || '/' || coalesce(p.actor_id, '') || '/' || m.start::text), 1, 24)), '0189', 'WXYZ'),
       p.level, p.tenant_id, p.actor_id, m.start, m.start + interval '1 month', 'USD', p.cap, 'deploy/compose control-budget-setup'
FROM (VALUES ('platform', NULL::text, NULL::text, :platform_cap::bigint),
             ('tenant', 'tenant_a', NULL::text, :tenant_cap::bigint),
             ('actor', 'tenant_a', 'user_a', :actor_cap::bigint)) AS p(level, tenant_id, actor_id, cap)
CROSS JOIN (SELECT date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + n * interval '1 month' AS start
            FROM generate_series(0, 1) AS n) AS m
ON CONFLICT (level, COALESCE(tenant_id, ''), COALESCE(actor_id, ''), period_start) DO NOTHING;

SELECT level, coalesce(tenant_id, '-') AS tenant, coalesce(actor_id, '-') AS actor,
       to_char(period_start AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS period_start,
       to_char(period_end AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS period_end,
       currency, cap_amount, allocated
FROM budget_pools
WHERE period_end > now()
ORDER BY period_start, level;
