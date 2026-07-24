-- fct_lp_market_session — the analytical backbone: one row per market-session,
-- enriched with the ET trading day, sport, market type, and the P&L decomposition
-- that the whole LP analysis turns on (capture = the repeatable edge; residual = luck).
--
-- Materialized as a TABLE (override the marts incremental default): the LP logs are
-- small (hundreds of sessions) and fully re-derivable each run, so there's nothing to
-- amortize with incremental/Iceberg machinery.

{{ config(materialized='table') }}

with sessions as (

    select * from {{ ref('stg_lp_sessions') }}

)

select
    session_at,
    -- ET calendar day = the analysis unit (games + our activity follow US hours).
    cast(with_timezone(session_at, 'UTC') at time zone 'America/New_York' as date) as et_day,
    market_ticker,

    -- Sport + market structure from the CANONICAL shared classifier (ml/lp/classify.py ->
    -- dbt/macros/classify.sql). Using the macro is what fixed the review's silent join bug:
    -- club-soccer fills used to fall to sport='OTHER' here (the CASE lacked MLS/LigaMX/...),
    -- so they never matched their capture-side verdict. Both marts now classify identically.
    {{ classify_sport('market_ticker') }} as sport,
    {{ classify_market_type('market_ticker') }} as market_type,

    minutes,
    n_fills,
    fills_per_min,

    -- The decomposition: net = capture + residual - fees.
    kalshi_gross - fees as net_pnl,
    net_cash           as spread_capture,   -- the repeatable edge
    kalshi_gross - net_cash as residual,    -- settlement luck (both-way)
    kalshi_gross,
    fees,

    max_abs_inv,
    avg_spread_c,
    mean_markout_c,
    config_version
from sessions
