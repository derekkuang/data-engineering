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

)

select * from features
