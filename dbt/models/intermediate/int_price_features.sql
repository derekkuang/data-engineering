-- Intermediate: price/volume features per (asset_id, event_at), computed for a
-- 15-minute DIRECTIONAL target. Materialized as a view (see dbt_project.yml).
--
-- POINT-IN-TIME DISCIPLINE (the whole point of this model):
--   * Every rolling window uses an EXPLICIT backward frame
--       rows between N preceding and current row
--     never the default cumulative frame, and never ... following. So a feature
--     at time T sees only bars with event_at <= T.
--   * Every window is partition by asset_id — BTC features never see ETH rows.
--
-- Row-based frames assume contiguous 1-minute bars. Coinbase BTC/ETH have ~0.7%
-- missing minutes, so "15 preceding rows" can occasionally span slightly more
-- than 15 wall-clock minutes. Accepted as a known v1 simplification; the exact
-- fix (a per-asset minute spine + forward-fill) is documented as future work.

with staged as (

    select * from {{ ref('stg_coinbase_ohlcv') }}

),

-- 1) Lagged closes for multi-horizon returns. lag() ignores the frame, so the
--    partition+order alone is enough here.
lags as (

    select
        *,
        lag(close_price, 1)  over w as prev_close,
        lag(close_price, 5)  over w as close_5m_ago,
        lag(close_price, 15) over w as close_15m_ago,
        lag(close_price, 60) over w as close_60m_ago
    from staged
    window w as (partition by asset_id order by event_at)

),

-- 2) Per-bar building blocks (row-level, no windows): returns, RSI gain/loss,
--    true range, and the log terms for the range-based vol estimators.
per_bar as (

    select
        asset_id,
        event_at,
        open_price,
        high_price,
        low_price,
        close_price,
        volume,

        -- multi-horizon log returns
        ln(close_price / nullif(prev_close, 0))     as log_return_1m,
        ln(close_price / nullif(close_5m_ago, 0))   as log_return_5m,
        ln(close_price / nullif(close_15m_ago, 0))  as log_return_15m,
        ln(close_price / nullif(close_60m_ago, 0))  as log_return_60m,

        -- RSI inputs (SMA-based RSI; Wilder/EMA smoothing is recursive and
        -- awkward in SQL — the difference is negligible for ML)
        greatest(close_price - prev_close, 0)       as gain,
        greatest(prev_close - close_price, 0)       as loss,

        -- ATR input
        greatest(
            high_price - low_price,
            abs(high_price - prev_close),
            abs(low_price - prev_close)
        )                                           as true_range,

        -- intrabar range as a % of price
        (high_price - low_price) / nullif(close_price, 0) as hl_range_pct,

        -- range-based volatility building blocks
        power(ln(high_price / nullif(low_price, 0)), 2)   as hl_log_sq,
        0.5 * power(ln(high_price / nullif(low_price, 0)), 2)
          - (2 * ln(2) - 1) * power(ln(close_price / nullif(open_price, 0)), 2)
                                                    as gk_bar,

        -- volume-derived
        close_price * volume                        as dollar_volume,
        sign(close_price - open_price) * volume     as signed_volume,

        -- calendar (known at T → PIT-safe). day_of_week: 1=Mon .. 7=Sun.
        hour(event_at) * 60 + minute(event_at)      as minute_of_day,
        hour(event_at)                              as hour_of_day,
        day_of_week(event_at)                       as day_of_week,
        sin(2 * pi() * (hour(event_at) * 60 + minute(event_at)) / 1440.0) as minute_sin,
        cos(2 * pi() * (hour(event_at) * 60 + minute(event_at)) / 1440.0) as minute_cos
    from lags

),

-- 3) Rolling aggregates over EXPLICIT backward frames (the PIT-critical part).
rolling as (

    select
        *,

        -- realized volatility = rolling std of 1-min log returns
        stddev_samp(log_return_1m) over (partition by asset_id order by event_at
            rows between 14 preceding and current row) as rv_15m,
        stddev_samp(log_return_1m) over (partition by asset_id order by event_at
            rows between 29 preceding and current row) as rv_30m,
        stddev_samp(log_return_1m) over (partition by asset_id order by event_at
            rows between 59 preceding and current row) as rv_60m,

        -- Parkinson & Garman-Klass volatility (use the OHLC we already have)
        sqrt(avg(hl_log_sq) over (partition by asset_id order by event_at
            rows between 29 preceding and current row) / (4 * ln(2))) as parkinson_30m,
        sqrt(greatest(avg(gk_bar) over (partition by asset_id order by event_at
            rows between 29 preceding and current row), 0))           as garman_klass_30m,

        -- ATR (SMA-based)
        avg(true_range) over (partition by asset_id order by event_at
            rows between 13 preceding and current row) as atr_14,

        -- moving averages + rolling std for trend / mean-reversion
        avg(close_price) over (partition by asset_id order by event_at
            rows between 19 preceding and current row) as sma_20,
        avg(close_price) over (partition by asset_id order by event_at
            rows between 59 preceding and current row) as sma_60,
        stddev_samp(close_price) over (partition by asset_id order by event_at
            rows between 19 preceding and current row) as std_20,

        -- RSI rolling averages
        avg(gain) over (partition by asset_id order by event_at
            rows between 13 preceding and current row) as avg_gain_14,
        avg(loss) over (partition by asset_id order by event_at
            rows between 13 preceding and current row) as avg_loss_14,

        -- volume baseline
        avg(volume) over (partition by asset_id order by event_at
            rows between 19 preceding and current row) as vol_sma_20
    from per_bar

)

-- 4) Final ratios that need the rolling aggregates.
select
    asset_id,
    event_at,
    close_price,

    -- returns
    log_return_1m,
    log_return_5m,
    log_return_15m,
    log_return_60m,

    -- realized / range-based volatility
    rv_15m,
    rv_30m,
    rv_60m,
    rv_15m / nullif(rv_60m, 0)                as rv_ratio_15_60,
    parkinson_30m,
    garman_klass_30m,

    -- range / ATR
    hl_range_pct,
    atr_14,

    -- momentum
    case
        when avg_gain_14 is null then null
        when avg_loss_14 = 0 and avg_gain_14 = 0 then 50.0
        when avg_loss_14 = 0 then 100.0
        else 100 - (100 / (1 + avg_gain_14 / avg_loss_14))
    end                                       as rsi_14,
    close_price / nullif(sma_20, 0) - 1       as dist_sma_20,
    close_price / nullif(sma_60, 0) - 1       as dist_sma_60,

    -- mean reversion (Bollinger z-score)
    (close_price - sma_20) / nullif(std_20, 0) as bb_z_20,

    -- volume
    volume,
    volume / nullif(vol_sma_20, 0)            as rel_volume_20,
    dollar_volume,
    signed_volume,

    -- calendar
    minute_of_day,
    hour_of_day,
    day_of_week,
    minute_sin,
    minute_cos
from rolling
