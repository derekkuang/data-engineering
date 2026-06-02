-- fct_features_pit — the point-in-time feature store (CROWN JEWEL).
--
-- The curated, consumer-facing table the ML model and dashboard read from. Every
-- row is guaranteed point-in-time-correct: it contains only information knowable
-- at event_at (the upstream int_price_features uses only backward-looking windows;
-- the forward-looking label is computed separately at train time and is NOT here).
--
-- Materialized as an INCREMENTAL ICEBERG table:
--   * incremental  -> each run processes only bars newer than what's stored,
--                     so cost scales with NEW data, not total data.
--   * iceberg+merge -> upsert on (asset_id, event_at): re-running an overlapping
--                     window updates in place instead of duplicating. Athena's
--                     classic Hive tables can't do row-level merge; Iceberg can.
--   * --full-refresh rebuilds the whole table deterministically.

{{ config(
    materialized='incremental',
    incremental_strategy='merge',
    unique_key=['asset_id', 'event_at'],
    table_type='iceberg',
    partitioned_by=['asset_id']
) }}

with features as (

    select * from {{ ref('int_price_features') }}

    {% if is_incremental() %}
    -- Only bars newer than the latest one already in this table. {{ this }} is
    -- fct_features_pit's own existing data.
    where event_at > (select max(event_at) from {{ this }})
    {% endif %}

),

-- Kalshi implied probability is BTC-only, so it is tagged asset_id='BTC-USD' and
-- joined on (asset_id, event_at) — joining on event_at alone would wrongly attach
-- BTC's probability to ETH rows. The value is the market price AT event_at, so it
-- is point-in-time-safe (no look-ahead), same contract as the price features.
-- ETH rows simply get NULL (there is no KXETH 15-min market here).
kalshi as (

    select
        'BTC-USD'           as asset_id,
        event_at,
        kalshi_implied_prob,
        kalshi_mid_price,
        kalshi_spread
    from {{ ref('int_kalshi_implied_prob') }}

)

select
    f.*,
    k.kalshi_implied_prob,
    k.kalshi_mid_price,
    k.kalshi_spread
from features f
left join kalshi k
    on  f.asset_id = k.asset_id
    and f.event_at = k.event_at
