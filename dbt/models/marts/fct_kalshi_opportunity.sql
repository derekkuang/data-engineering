-- fct_kalshi_opportunity — the LP opportunity landscape: one row per (series, snapshot day),
-- ranked by gross spread-capture, carrying the maker-fee flag + near-money spread + depth.
--
-- capture = sum(volume_24h * spread / 2) = the GROSS half-spread dollars/day a maker could
-- earn if they won every fill (an UPPER BOUND + a sound RELATIVE ranking; see
-- ml/lp/lp_market_screen.py). It is NOT realized P&L (that lives in fct_lp_daily) and says
-- nothing about toxicity — a wide spread can be wide because flow is informed. has_maker_fee
-- flags series where a Feb-2026 maker fee (0.0175*C*P*(1-P)) eats into that half-spread.
--
-- Materialized as a TABLE (override the marts incremental default): universe snapshots are
-- small + fully re-derivable each run, like fct_lp_market_session — no Iceberg/merge needed.

{{ config(materialized='table') }}

with universe as (

    select
        *,
        cast(snapshot_at as date) as snapshot_day   -- UTC, aligns with the dt partition
    from {{ ref('stg_kalshi_universe') }}

),

agg as (

    select
        series_ticker,
        snapshot_day,
        max(category)                                             as category,
        max(fee_type)                                             as fee_type,
        count(*)                                                  as n_markets,
        sum(if(is_near_money, 1, 0))                              as n_near_money_markets,
        sum(volume_24h)                                           as volume_24h,
        sum(open_interest)                                        as open_interest,
        sum(liquidity)                                            as depth,        -- $ resting book
        approx_percentile(spread_c, 0.5)                          as median_spread_c,
        approx_percentile(if(is_near_money, spread_c, null), 0.5) as median_near_money_spread_c,
        sum(volume_24h * spread / 2.0)                            as spread_capture
    from universe
    group by series_ticker, snapshot_day

)

select
    series_ticker,
    snapshot_day,
    category,
    fee_type,
    (fee_type = 'quadratic_with_maker_fees')                  as has_maker_fee,
    n_markets,
    n_near_money_markets,
    volume_24h,
    open_interest,
    depth,
    median_spread_c,
    median_near_money_spread_c,
    spread_capture,
    case when volume_24h > 0 then 100.0 * spread_capture / volume_24h end as capture_c_per_contract
from agg
order by snapshot_day, spread_capture desc
