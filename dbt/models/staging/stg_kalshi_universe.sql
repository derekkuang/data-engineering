-- Staging: clean the Kalshi universe snapshot (one row per market per snapshot).
-- View (see dbt_project.yml). Rename/derive, dedupe, drop unusable rows.
--
-- Each row is one open market's point-in-time quote. Derives mid/spread + the near-money
-- and retail-spread-band flags the LP screen uses (ml/lp/soccer_screen.py: near-money mid
-- in [0.15, 0.85]; retail band = 2-15c spread). Drops one-sided / no-flow markets so every
-- downstream row is something a maker could actually rest inside.

with source as (

    select * from {{ source('kalshi', 'kalshi_universe') }}

),

cleaned as (

    select
        snapshot_at,
        market_ticker,
        event_ticker,
        series_ticker,
        category,
        status,
        open_time,
        close_time,
        yes_bid,
        yes_ask,
        (yes_bid + yes_ask) / 2.0             as mid_price,
        yes_ask - yes_bid                     as spread,        -- DOLLARS
        (yes_ask - yes_bid) * 100.0           as spread_c,      -- CENTS
        volume_24h,
        open_interest,
        liquidity,
        yes_bid_size,
        yes_ask_size,
        least(yes_bid_size, yes_ask_size)     as min_touch_depth,
        ((yes_bid + yes_ask) / 2.0 between 0.15 and 0.85)  as is_near_money,
        ((yes_ask - yes_bid) * 100.0 between 2 and 15)     as in_retail_band,
        fee_type,
        (fee_type = 'quadratic_with_maker_fees')           as has_maker_fee,
        ingested_at,
        dt                                    as partition_date
    from source
    where yes_bid is not null
      and yes_ask is not null
      and yes_ask > yes_bid       -- two-sided only
      and volume_24h > 0          -- real flow only

),

deduped as (

    -- Idempotent overwrite makes dupes unlikely, but enforce the grain so downstream
    -- models can trust one row per (market, snapshot).
    select
        *,
        row_number() over (
            partition by market_ticker, snapshot_at
            order by ingested_at desc
        ) as _row_num
    from cleaned

)

select
    snapshot_at,
    market_ticker,
    event_ticker,
    series_ticker,
    category,
    status,
    open_time,
    close_time,
    yes_bid,
    yes_ask,
    mid_price,
    spread,
    spread_c,
    volume_24h,
    open_interest,
    liquidity,
    yes_bid_size,
    yes_ask_size,
    min_touch_depth,
    is_near_money,
    in_retail_band,
    fee_type,
    has_maker_fee,
    ingested_at,
    partition_date
from deduped
where _row_num = 1
