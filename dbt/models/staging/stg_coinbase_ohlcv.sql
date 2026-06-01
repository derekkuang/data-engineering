-- Staging: thin, 1:1 cleanup of the raw Coinbase OHLCV external table.
-- Materialized as a view (see dbt_project.yml) — always fresh, no stored copy.
-- Rename/cast, drop impossible bars, enforce one row per (asset_id, event_at).
-- No joins, no aggregations, no feature logic — that lives in intermediate/marts.

with source as (

    select * from {{ source('coinbase', 'coinbase_ohlcv') }}

),

renamed as (

    select
        asset_id,
        event_at,
        open        as open_price,
        high        as high_price,
        low         as low_price,
        close       as close_price,
        volume,
        ingested_at,
        dt          as partition_date
    from source
    where close is not null   -- a bar with no close is unusable
      and volume >= 0         -- volume is never negative
      and high >= low         -- impossible bar otherwise

),

deduped as (

    -- Idempotent overwrite makes dupes unlikely, but enforce the grain here so
    -- every downstream model can trust exactly one row per (asset, minute).
    select
        *,
        row_number() over (
            partition by asset_id, event_at
            order by ingested_at desc
        ) as _row_num
    from renamed

)

select
    asset_id,
    event_at,
    open_price,
    high_price,
    low_price,
    close_price,
    volume,
    ingested_at,
    partition_date
from deduped
where _row_num = 1
