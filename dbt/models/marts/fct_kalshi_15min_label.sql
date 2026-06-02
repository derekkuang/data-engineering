-- fct_kalshi_15min_label — the forward 15-min directional LABEL.
--
-- One row per SETTLED KXBTC15M window. `result` is the settlement outcome, known
-- only AFTER the window closes, so it is FORWARD-LOOKING and deliberately kept OUT
-- of fct_features_pit. It is joined to features ONLY at train time, on the window's
-- decision minute (window_open_at = the feature row's event_at).
--
-- Materialized as a VIEW (override the marts incremental default): it's small
-- (~one row per 15-min window) and pure derivation, so there's nothing to amortize.

{{ config(materialized='view') }}

with windows as (

    select
        market_ticker,
        window_open_at,
        window_close_at,
        max(result) as result   -- result is constant per market; max() collapses the candles
    from {{ ref('stg_kalshi_btc_15min') }}
    group by market_ticker, window_open_at, window_close_at

)

select
    market_ticker,
    window_open_at,
    window_close_at,
    result,
    case
        when result = 'yes' then 1   -- BTC up over the window
        when result = 'no'  then 0   -- BTC not up
    end as label_up
from windows
where result in ('yes', 'no')   -- settled windows only; drop in-progress ('')
