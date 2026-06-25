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

    -- Sport from the ticker prefix (prefix match is unambiguous: KXWNBA != KXNBA).
    case
        when market_ticker like 'KXWNBA%' then 'WNBA'
        when market_ticker like 'KXWC%'   then 'WC'
        when market_ticker like 'KXMLB%'  then 'MLB'
        when market_ticker like 'KXNCAA%' then 'NCAA'
        when market_ticker like 'KXNBA%'  then 'NBA'
        when market_ticker like 'KXNHL%'  then 'NHL'
        when market_ticker like 'KXNFL%'  then 'NFL'
        when market_ticker like 'KXATP%'  then 'ATP'
        when market_ticker like 'KXITF%'  then 'ITF'
        when market_ticker like 'KXWTA%'  then 'WTA'
        else 'OTHER'
    end as sport,

    -- Market structure. TOTAL/SPREAD are the mean-reverting allowlist; the rest are
    -- directional (legacy data) and kept only so the breakdown can show why they lost.
    case
        when market_ticker like '%TOTAL%'   then 'TOTAL'
        when market_ticker like '%SPREAD%'  then 'SPREAD'
        when market_ticker like '%MATCH%'   then 'MATCH'
        when market_ticker like '%WINNER%'  then 'WINNER'
        when market_ticker like '%MENTION%' then 'MENTION'
        when market_ticker like '%GAME%'    then 'GAME'
        else 'OTHER'
    end as market_type,

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
