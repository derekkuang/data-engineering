-- Staging: clean candle-level Kalshi KXBTC15M data (one row per market-minute).
-- View (see dbt_project.yml). Rename/derive, dedupe, drop unusable rows.
--
-- Each row is one 1-minute candlestick of a 15-min "BTC price up in next 15 mins?"
-- market: implied_prob = market price (0..1) = P(up over the window); spread =
-- yes_ask - yes_bid = the trading cost; result = the window's settlement.
-- NOTE: result is FORWARD-LOOKING — downstream it becomes the LABEL only, never a
-- point-in-time feature.

with source as (

    select * from {{ source('kalshi', 'kalshi_btc_15min') }}

),

cleaned as (

    select
        market_ticker,
        series_ticker,
        window_open_at,
        window_close_at,
        event_at,
        implied_prob_close                       as implied_prob,
        implied_prob_mean,
        yes_bid_close                            as yes_bid,
        yes_ask_close                            as yes_ask,
        (yes_bid_close + yes_ask_close) / 2.0    as mid_price,
        yes_ask_close - yes_bid_close            as spread,
        volume,
        open_interest,
        result,
        ingested_at,
        dt                                       as partition_date
    from source
    where implied_prob_close is not null   -- need a usable price to be a feature

),

deduped as (

    -- Idempotent overwrite makes dupes unlikely, but enforce the grain so every
    -- downstream model can trust one row per (market, minute).
    select
        *,
        row_number() over (
            partition by market_ticker, event_at
            order by ingested_at desc
        ) as _row_num
    from cleaned

)

select
    market_ticker,
    series_ticker,
    window_open_at,
    window_close_at,
    event_at,
    implied_prob,
    implied_prob_mean,
    yes_bid,
    yes_ask,
    mid_price,
    spread,
    volume,
    open_interest,
    result,
    ingested_at,
    partition_date
from deduped
where _row_num = 1
