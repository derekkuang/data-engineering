-- fct_toxicity_by_family — the toxicity scoreboard: one row per (sport, market_type,
-- capture day), aggregating the flow-signed markout label from fct_ws_markout.
--
-- KEY FILTER: only flow-bearing snapshots (signed_flow_1m != 0) enter the aggregates.
-- flow_signed_markout_c is 0 BY CONSTRUCTION when there was no net flow, so including
-- flowless rows would drag every mean toward 0 and bias families benign.
--
-- avg_flow_markout_c > 0  = net taker flow predicted the next move = TOXIC for a maker.
-- avg_flow_markout_c ~ 0  = uninformed flow = the makeable regime.
-- Known-toxic families (ITF, MLB/NBA GAME) are captured deliberately as positive controls:
-- if they don't read toxic here, the instrument is broken, not the market benign.
--
-- The day grain exists because days are the honest resampling unit — ml/lp/edge_verdict.py
-- draws day-block bootstrap CIs from this table. Table: small + re-derivable.

{{ config(materialized='table') }}

with markout as (

    select * from {{ ref('fct_ws_markout') }}

),

labeled as (

    select
        -- ET calendar day (NOT UTC): a US-evening game slate runs past UTC midnight, so a UTC
        -- date splits ONE slate into two "days" and narrows the day-block bootstrap CI with
        -- pseudo-independent halves. ET keeps a slate as one resampling unit. Matches
        -- fct_lp_market_session.et_day so the two sides' day counts are comparable.
        cast(with_timezone(snapshot_at, 'UTC') at time zone 'America/New_York' as date)
            as capture_day,
        market_ticker,

        -- Canonical shared classifier (ml/lp/classify.py -> dbt/macros/classify.sql); the
        -- LP-fills mart uses the SAME macro, so the two sides' families join by construction.
        {{ classify_sport('market_ticker') }} as sport,
        {{ classify_market_type('market_ticker') }} as market_type,

        spread_c,
        signed_flow_1m,
        flow_signed_markout_c,
        jump_pickoff_c
    from markout

)

select
    sport,
    market_type,
    capture_day,
    count(*)                                              as n_snapshots,
    count(distinct market_ticker)                         as n_markets,
    sum(if(signed_flow_1m != 0, 1, 0))                    as n_flow_obs,
    avg(if(signed_flow_1m != 0, flow_signed_markout_c))   as avg_flow_markout_c,
    approx_percentile(
        if(signed_flow_1m != 0, flow_signed_markout_c), 0.5
    )                                                     as med_flow_markout_c,
    -- flow-INDEPENDENT jump toxicity, over ALL snapshots (jumps happen without net flow): the
    -- expected per-snapshot pick-off a touch-resting maker faces, and how often it exceeds the
    -- spread. The known-jumpy controls (ITF/MATCH, *_GAME) must read HIGH here even when their
    -- flow_markout reads benign (the review's construct-validity fix).
    avg(jump_pickoff_c)                                   as avg_jump_pickoff_c,
    avg(if(jump_pickoff_c > 0, 1.0, 0.0))                 as frac_pickoff,
    avg(spread_c)                                         as avg_spread_c
from labeled
group by sport, market_type, capture_day
order by capture_day, sport, market_type
