-- Staging: thin 1:1 cleanup of the raw LP sessions external table.
-- Materialized as a view (see dbt_project.yml) — always fresh, no stored copy.
-- Cast/rename, basic sanity filters, enforce one row per (session_at, market).
-- No enrichment (sport/type/et_day) or P&L derivation — that lives in the marts.

with source as (

    select * from {{ source('lp', 'lp_sessions') }}

),

cleaned as (

    select
        session_at,
        market_ticker,
        minutes,
        n_fills,
        fills_per_min,
        net_cash,
        kalshi_gross,
        fees,
        max_abs_inv,
        pnl_min,
        pnl_max,
        avg_spread_c,
        mean_markout_c,
        coalesce(config_version, 'legacy') as config_version,
        ingested_at,
        dt as partition_date
    from source
    where minutes >= 0
      and n_fills >= 0

),

deduped as (

    -- Idempotent partition overwrite makes dupes unlikely, but enforce the grain so
    -- every downstream model can trust one row per (session_at, market).
    select
        *,
        row_number() over (
            partition by session_at, market_ticker
            order by ingested_at desc
        ) as _row_num
    from cleaned

)

select
    session_at,
    market_ticker,
    minutes,
    n_fills,
    fills_per_min,
    net_cash,
    kalshi_gross,
    fees,
    max_abs_inv,
    pnl_min,
    pnl_max,
    avg_spread_c,
    mean_markout_c,
    config_version,
    ingested_at,
    partition_date
from deduped
where _row_num = 1
