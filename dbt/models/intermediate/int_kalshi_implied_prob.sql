-- Intermediate: per wall-clock minute, the ACTIVE market's implied probability.
-- Materialized as a view (see dbt_project.yml).
--
-- POINT-IN-TIME mapping (the whole purpose of this model):
--   minute T -> the market whose 15-min window CONTAINS T
--   (window_open_at <= T < window_close_at). The strict upper bound picks the
--   just-opened market at a boundary (the live "next 15 min" forecast), never the
--   one settling. The value is the market price AT T, so joining it into the
--   per-minute feature store is point-in-time-safe — no look-ahead.

with in_window as (

    select *
    from {{ ref('stg_kalshi_btc_15min') }}
    where event_at >= window_open_at
      and event_at <  window_close_at

),

ranked as (

    -- One active market per minute. If two windows ever overlap a minute, prefer
    -- the newer window (the current forecast).
    select
        *,
        row_number() over (
            partition by event_at
            order by window_open_at desc
        ) as rn
    from in_window

)

select
    event_at,
    market_ticker     as kalshi_market_ticker,
    window_open_at    as kalshi_window_open_at,
    window_close_at   as kalshi_window_close_at,
    implied_prob      as kalshi_implied_prob,
    mid_price         as kalshi_mid_price,
    spread            as kalshi_spread,
    yes_bid           as kalshi_yes_bid,
    yes_ask           as kalshi_yes_ask
from ranked
where rn = 1
