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
        cast(snapshot_at as date) as capture_day,
        market_ticker,

        case
            when market_ticker like 'KXWC%'                                   then 'WC'
            when market_ticker like 'KXMLS%'                                  then 'MLS'
            when market_ticker like 'KXLIGAMX%'                               then 'LIGAMX'
            when market_ticker like 'KXBRASILEIRO%'                           then 'BRASILEIRO'
            when market_ticker like 'KXALLSVENSKAN%'
              or market_ticker like 'KXELITESERIEN%'
              or market_ticker like 'KXDENSUPERLIGA%'                         then 'SCANDI'
            when market_ticker like 'KXEPL%'                                  then 'EPL'
            when market_ticker like 'KXLALIGA%'                               then 'LALIGA'
            when market_ticker like 'KXSERIEA%'                               then 'SERIEA'
            when market_ticker like 'KXBUNDESLIGA%'                           then 'BUNDESLIGA'
            when market_ticker like 'KXLIGUE1%'                               then 'LIGUE1'
            when market_ticker like 'KXUCL%' or market_ticker like 'KXUEL%'   then 'UCL_UEL'
            when market_ticker like 'KXEREDIVISIE%'
              or market_ticker like 'KXEFLCHAMPIONSHIP%'
              or market_ticker like 'KXCOPADOBRASIL%'
              or market_ticker like 'KXSAUDIPL%'                              then 'SOCCER_OTHER'
            when market_ticker like 'KXWNBA%'                                 then 'WNBA'
            when market_ticker like 'KXMLB%'                                  then 'MLB'
            when market_ticker like 'KXNCAA%'                                 then 'NCAA'
            when market_ticker like 'KXNBA%'                                  then 'NBA'
            when market_ticker like 'KXNHL%'                                  then 'NHL'
            when market_ticker like 'KXNFL%'                                  then 'NFL'
            when market_ticker like 'KXATP%'                                  then 'ATP'
            when market_ticker like 'KXITF%'                                  then 'ITF'
            when market_ticker like 'KXWTA%'                                  then 'WTA'
            else 'OTHER'
        end as sport,

        case
            when market_ticker like '%TOTAL%'   then 'TOTAL'
            when market_ticker like '%SPREAD%'  then 'SPREAD'
            when market_ticker like '%MATCH%'   then 'MATCH'
            when market_ticker like '%WINNER%'  then 'WINNER'
            when market_ticker like '%GAME%'    then 'GAME'
            else 'OTHER'
        end as market_type,

        spread_c,
        signed_flow_1m,
        flow_signed_markout_c
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
    avg(spread_c)                                         as avg_spread_c
from labeled
group by sport, market_type, capture_day
order by capture_day, sport, market_type
