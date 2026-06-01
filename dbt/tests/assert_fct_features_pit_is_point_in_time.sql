-- PIT-CORRECTNESS PROOF — the project's signature test.
--
-- Independently recompute rel_volume_20 for a sample of stored rows using ONLY
-- raw bars with event_at <= the row's timestamp (strictly backward), then assert
-- it equals the stored feature. If any rolling window in int_price_features had
-- look-ahead (pulled in future bars), the stored value would diverge from this
-- backward-only recompute and rows would surface here.
--
-- Singular test: PASSES when it returns zero rows.

with sampled as (

    -- 5 latest rows per asset (well past the 60-bar warmup) to check.
    select asset_id, event_at, volume, rel_volume_20
    from (
        select
            asset_id,
            event_at,
            volume,
            rel_volume_20,
            row_number() over (partition by asset_id order by event_at desc) as rn_sample
        from {{ ref('fct_features_pit') }}
    )
    where rn_sample <= 5

),

-- For each sampled row, rank raw bars at-or-before its timestamp, most recent
-- first. The `r.event_at <= s.event_at` join condition IS the PIT enforcement:
-- the recompute is structurally incapable of seeing the future.
ranked_raw as (

    select
        s.asset_id,
        s.event_at      as ref_event_at,
        s.volume        as volume_t,
        s.rel_volume_20 as stored_rel_volume_20,
        r.volume        as raw_volume,
        row_number() over (
            partition by s.asset_id, s.event_at
            order by r.event_at desc
        ) as rn
    from sampled s
    join {{ ref('stg_coinbase_ohlcv') }} r
      on r.asset_id = s.asset_id
     and r.event_at <= s.event_at

),

recompute as (

    -- Trailing 20-bar average volume up to and including T (matches the model's
    -- `rows between 19 preceding and current row` = 20 bars).
    select
        asset_id,
        ref_event_at,
        volume_t,
        stored_rel_volume_20,
        avg(case when rn <= 20 then raw_volume end) as vol_sma_20_recomputed
    from ranked_raw
    group by asset_id, ref_event_at, volume_t, stored_rel_volume_20

)

select
    asset_id,
    ref_event_at as event_at,
    stored_rel_volume_20,
    volume_t / nullif(vol_sma_20_recomputed, 0) as recomputed_rel_volume_20
from recompute
where abs(stored_rel_volume_20 - volume_t / nullif(vol_sma_20_recomputed, 0)) > 1e-9
