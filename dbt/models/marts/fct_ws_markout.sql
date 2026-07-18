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
-- H (label horizon) = 30s to match the maker's MARKOUT_HORIZON_S; tune here. Materialized as a
-- table — small + fully re-derivable, like the other marts.

{{ config(materialized='table') }}

{% set horizon_s = 30 %}
{% set window_s = 120 %}   -- give up if no later obs within this extra window (market went quiet)

with f as (

    select * from {{ ref('stg_ws_features') }}

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
    case
        when signed_flow_1m > 0 then (fwd_mid - mid) * 100.0
        when signed_flow_1m < 0 then -(fwd_mid - mid) * 100.0
        else 0.0
    end as flow_signed_markout_c
from paired
where _rn = 1 and fwd_mid is not null   -- only snapshots with a forward observation
