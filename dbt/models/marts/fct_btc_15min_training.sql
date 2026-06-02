-- fct_btc_15min_training — the leakage-free trainable table (one row per settled
-- 15-min window). This is what the ML model + walk-forward backtest read.
--
-- DECISION-TIME ALIGNMENT (the PIT-critical choice — documented so the model can
-- trust the table):
--   * A Kalshi window [W, W+15] settles on price[W+15] vs price[W]. That window's
--     market only exists from W onward, so its first OBSERVABLE price is its first
--     1-min candle (~W+1). decision_at = that first in-window minute.
--   * Features come from fct_features_pit at event_at = decision_at (BTC): the
--     price/vol/momentum features AND kalshi_implied_prob, all known AS OF
--     decision_at. The label settles at W+15 (~14 min later) → no look-ahead.
--   * So at decision_at the model and the market make the SAME call on the SAME
--     target: model prob vs kalshi_implied_prob (the benchmark), apples to apples.
--
-- NOTE: here event_at (from fct_features_pit) IS the decision minute (~W+1), not W.
-- Inner joins drop the rare window missing a Coinbase bar at decision_at (~0.7%).
--
-- View (overrides the marts incremental default): small, always-fresh derivation.
-- The ML session can UNLOAD it to S3 Parquet for SageMaker / snapshot as needed.

{{ config(materialized='view') }}

with decision as (

    -- first observable minute per window = the earliest in-window Kalshi candle
    select
        kalshi_window_open_at as window_open_at,
        min(event_at)         as decision_at
    from {{ ref('int_kalshi_implied_prob') }}
    group by kalshi_window_open_at

)

select
    lbl.market_ticker,
    lbl.window_open_at,           -- W: window start (settlement reference start)
    lbl.window_close_at,          -- W+15: settlement reference end
    lbl.label_up,                 -- TARGET: 1 if BTC up over the window
    f.*                           -- features at the decision minute; f.event_at = decision_at (~W+1)
from {{ ref('fct_kalshi_15min_label') }} lbl
join decision d
    on d.window_open_at = lbl.window_open_at
join {{ ref('fct_features_pit') }} f
    on f.asset_id = 'BTC-USD'
   and f.event_at = d.decision_at
