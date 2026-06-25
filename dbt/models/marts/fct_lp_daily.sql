-- fct_lp_daily — the OOS running tally as a model: one row per ET trading day with
-- net P&L, the capture/residual split, fill volume, and the fill-weighted markout
-- (the toxicity signal). This is what the dashboard and the day-block CIs read.
--
-- markout lives in the fills, the P&L in the sessions, so we aggregate each and join
-- on et_day; fills inherit et_day by joining back to the session backbone.
--
-- Materialized as a TABLE (small, fully re-derivable) — see fct_lp_market_session.

{{ config(materialized='table') }}

with sessions as (

    select * from {{ ref('fct_lp_market_session') }}

),

fills as (

    select * from {{ ref('stg_lp_fills') }}

),

daily_pnl as (

    select
        et_day,
        count(*)                          as n_sessions,
        count(distinct market_ticker)     as n_markets,
        sum(n_fills)                      as n_fills,
        sum(net_pnl)                      as net_pnl,
        sum(spread_capture)               as spread_capture,
        sum(residual)                     as residual,
        sum(fees)                         as fees,
        array_join(array_agg(distinct config_version), ',') as config_versions
    from sessions
    group by et_day

),

daily_markout as (

    -- join fills to the session backbone to attribute each fill's markout to its ET day
    select
        s.et_day,
        avg(f.markout_c)  as markout_c,
        count(*)          as n_marked_fills
    from fills as f
    inner join sessions as s
        on f.session_at = s.session_at
       and f.market_ticker = s.market_ticker
    where f.markout_c is not null
    group by s.et_day

)

select
    p.et_day,
    p.n_sessions,
    p.n_markets,
    p.n_fills,
    p.net_pnl,
    p.spread_capture,
    p.residual,
    p.fees,
    m.markout_c,
    m.n_marked_fills,
    -- per-fill capture in cents (the headline economics)
    case when p.n_fills > 0 then 100.0 * p.spread_capture / p.n_fills end as capture_c_per_fill,
    p.config_versions
from daily_pnl as p
left join daily_markout as m on p.et_day = m.et_day
order by p.et_day
