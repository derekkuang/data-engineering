-- Staging: clean the WS microstructure-feature capture (one row per market per snapshot).
-- View. Rename/dedup, drop rows without a usable mid. These are the features the toxicity /
-- selection model trains on; fct_ws_markout adds the forward-markout label.

with source as (

    select * from {{ source('ws', 'ws_features') }}

),

deduped as (

    select
        snapshot_at,
        market_ticker,
        bid,
        ask,
        mid,
        spread_c,
        imbalance,
        yes_depth,
        no_depth,
        depth_near,
        n_levels,
        trades_1m,
        vol_1m,
        taker_buy_frac,
        signed_flow_1m,
        midvol_1m,
        midmove_1m,
        dt as partition_date,
        row_number() over (
            partition by market_ticker, snapshot_at
            order by ingested_at desc
        ) as _row_num
    from source
    where mid is not null

)

select
    snapshot_at, market_ticker, bid, ask, mid, spread_c, imbalance, yes_depth, no_depth,
    depth_near, n_levels, trades_1m, vol_1m, taker_buy_frac, signed_flow_1m, midvol_1m,
    midmove_1m, partition_date
from deduped
where _row_num = 1
