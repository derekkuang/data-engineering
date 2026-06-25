-- Staging: thin 1:1 cleanup of the raw LP fills external table.
-- Materialized as a view (see dbt_project.yml) — always fresh, no stored copy.
-- Cast/rename + drop impossible prices. No dedup: the partition overwrite is
-- idempotent, and two genuine fills can share (time, side, price), so a row_number
-- dedup would wrongly collapse them. markout_c may be null (fills too close to the
-- session end to have a forward mid) — kept; downstream filters where needed.

with source as (

    select * from {{ source('lp', 'lp_fills') }}

)

select
    session_at,
    market_ticker,
    fill_at,
    book_side,
    price,
    mid_at_fill,
    mid_after,
    markout_c,
    ingested_at,
    dt as partition_date
from source
where price >= 0
  and price <= 1
  and book_side in ('bid', 'ask')
