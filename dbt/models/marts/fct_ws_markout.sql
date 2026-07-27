-- fct_ws_markout — the labeled toxicity / ML table: each microstructure snapshot joined
-- FORWARD to a later mid, so every feature row carries what happened next.
--
-- For each (market, snapshot_at) we find the nearest later snapshot in [t+H, t+H+window) for
-- the same market and take its mid. Then:
--   fwd_mid_move_c      = (fwd_mid - mid) * 100                     -- the next move, cents
--   flow_signed_markout = sign(signed_flow_1m) * fwd_mid_move_c     -- >0 => net taker flow
--                                                                      PREDICTED the move = toxic
-- flow_signed_markout is the market-level adverse-selection signal: if net taker flow reliably
-- precedes the mid moving the same way, a resting maker on the hit side gets picked off (the
-- thing that killed the basketball/baseball books). This is the label to (a) rank markets by
-- toxicity and (b) train the selection model on the microstructure features.
--
-- BUT flow_signed_markout is signed by trailing FLOW — it is BLIND to a jump that isn't preceded
-- by net taker flow (a GOAL, a tennis point), which is the dominant sports pick-off channel. So we
-- ALSO carry a flow-INDEPENDENT jump-toxicity metric:
--   jump_pickoff_c = max(0, |fwd_mid_move_c| - spread_c/2)
-- A two-sided maker resting at the touch captures the spread when the mid move stays within the
-- half-spread and is PICKED OFF for the excess when it gaps past it — regardless of flow direction.
-- Averaged over ALL snapshots (see fct_toxicity_by_family) this is the goal-jump toxicity the
-- flow-signed label cannot see; the KNOWN-jumpy controls (ITF points, moneyline GAMEs) must read
-- high on it.
--
-- H (label horizon) = 30s to match the maker's MARKOUT_HORIZON_S; tune here.
--
-- Materialized INCREMENTAL APPEND (was a full-rebuild `table`). This model self-joins
-- stg_ws_features to itself (each snapshot forward to a later mid), so a full rebuild re-scans
-- ALL capture history twice every night — which breaches the 1GB Athena workgroup scan cap as
-- the capture grows (~Oct). Incremental bounds it with TWO conditions on the `f` CTE:
--   * partition_date >= today-3  -> PRUNES the physical scan to the last few UTC partitions
--     (dt is the raw partition; snapshot_at is not, so a snapshot_at filter alone would still
--     scan everything). This is what actually caps the scan.
--   * snapshot_at > max(this)    -> DEDUP: append only strictly-new snapshots, so re-runs add
--     nothing (append has no row-merge). The trailing ~150s of a capture have no forward obs yet
--     (dropped as null); max() = the last STORED snapshot, so the next run naturally resumes just
--     before them and appends them once their forward mid has landed.
-- Both conditions apply to BOTH self-join sides (same CTE); g (the forward mid) is always
-- >= f + 30s and within ~150s, so it lives in the same pruned window. Append (not Iceberg merge)
-- so the transition needs no DROP of the existing Hive table. --full-refresh rebuilds all (a
-- deliberate, permissioned op). Caveat: a pipeline gap > the 3-day window would skip the gap's
-- partitions — the Step-5 dead-man's-switch alarms on a >3-day capture gap, and --full-refresh
-- recovers. fct_toxicity_by_family stays a full-rebuild table (see its header).

{{ config(materialized='incremental', incremental_strategy='append') }}

{% set horizon_s = 30 %}
{% set window_s = 120 %}   -- give up if no later obs within this extra window (market went quiet)
{% set scan_days = 3 %}    -- partition-prune window; wider than the daily gap, < a real outage

with f as (

    select * from {{ ref('stg_ws_features') }}

    {% if is_incremental() %}
    where partition_date >= date_format(current_date - interval '{{ scan_days }}' day, '%Y-%m-%d')
      and snapshot_at > (select max(snapshot_at) from {{ this }})
    {% endif %}

),

paired as (

    select
        f.snapshot_at,
        f.market_ticker,
        f.mid,
        f.spread_c,
        f.imbalance,
        f.depth_near,
        f.trades_1m,
        f.vol_1m,
        f.taker_buy_frac,
        f.signed_flow_1m,
        f.midvol_1m,
        f.midmove_1m,
        f.partition_date,
        g.mid as fwd_mid,
        g.snapshot_at as fwd_at,
        row_number() over (
            partition by f.market_ticker, f.snapshot_at
            order by g.snapshot_at
        ) as _rn
    from f
    left join f as g
        on  g.market_ticker = f.market_ticker
        and g.snapshot_at >= f.snapshot_at + interval '{{ horizon_s }}' second
        and g.snapshot_at <  f.snapshot_at + interval '{{ horizon_s + window_s }}' second

)

select
    snapshot_at,
    market_ticker,
    partition_date,
    mid,
    spread_c,
    imbalance,
    depth_near,
    trades_1m,
    vol_1m,
    taker_buy_frac,
    signed_flow_1m,
    midvol_1m,
    midmove_1m,
    fwd_mid,
    fwd_at,
    (fwd_mid - mid) * 100.0 as fwd_mid_move_c,
    abs((fwd_mid - mid) * 100.0) as abs_fwd_move_c,
    -- flow-INDEPENDENT jump toxicity: the part of the 30s move that runs PAST the half-spread a
    -- maker at the touch would capture = what a goal/point jump costs, whether or not flow led it.
    greatest(0.0, abs((fwd_mid - mid) * 100.0) - spread_c / 2.0) as jump_pickoff_c,
    case
        when signed_flow_1m > 0 then (fwd_mid - mid) * 100.0
        when signed_flow_1m < 0 then -(fwd_mid - mid) * 100.0
        else 0.0
    end as flow_signed_markout_c
from paired
where _rn = 1 and fwd_mid is not null   -- only snapshots with a forward observation
