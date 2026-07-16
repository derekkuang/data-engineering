# 08 — Kalshi universe opportunity snapshot

A daily point-in-time snapshot of the whole open Kalshi market universe, on the same
S3 → Glue → Athena → dbt stack as the rest of the platform. This is the "where can a maker
earn?" landscape as a warehouse layer: every open market's spread, depth, volume, and
`fee_type`, rolled up per series and ranked by gross spread-capture.

```
ingestion/kalshi_universe.py ──► s3://…/raw/kalshi_universe/dt=…/snapshot.parquet
                                                    │
                        Glue external table (partition projection on dt)
                                                    │
              dbt: stg_kalshi_universe (view) ─► fct_kalshi_opportunity (table)
```

Kalshi Pro (launched 2026-07-13) ships a *real-time* screener; this is the complementary
*historical/analytical* layer — daily snapshots accumulate so you can see how spreads,
volume, and the opportunity set move over time, and join it to realized markout later.

## 1. Land a snapshot to S3

```bash
# needs S3_BUCKET + AWS creds (the same crypto-de-pipeline role used elsewhere)
uv run python -m ingestion.kalshi_universe                 # land today's snapshot to S3
uv run python -m ingestion.kalshi_universe --dry-run       # fetch + field diagnostic, no write
uv run python -m ingestion.kalshi_universe --local-dir ./wh  # offline: write dt= Parquet locally
```

One Parquet file per `dt` (UTC date of the snapshot), overwritten on re-run, so re-runs the
same day are idempotent (last-snapshot-wins). `dt` lives in the S3 key, not the Parquet
columns. The job enumerates the open universe via `/events?with_nested_markets` (a handful of
pages) plus a single `/series` list call that carries `fee_type` for the whole universe (no
per-series fan-out); it never fans out per-market order books.

## 2. Create the Glue external table (one-time, in the Athena console)

The column list is the contract with `ingestion/kalshi_universe_storage.py`'s
`PARQUET_SCHEMA` — same order + types. Run under an **admin** identity (the pipeline user is
read-only on `crypto_raw`).

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS crypto_raw.kalshi_universe (
  snapshot_at     timestamp,
  market_ticker   string,
  event_ticker    string,
  series_ticker   string,
  category        string,
  status          string,
  open_time       timestamp,
  close_time      timestamp,
  yes_bid         double,
  yes_ask         double,
  volume_24h      double,
  open_interest   double,
  liquidity       double,
  yes_bid_size    double,
  yes_ask_size    double,
  fee_type        string,
  ingested_at     timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/kalshi_universe/'
TBLPROPERTIES (
  'projection.enabled'          = 'true',
  'projection.dt.type'          = 'date',
  'projection.dt.format'        = 'yyyy-MM-dd',
  'projection.dt.range'         = '2026-07-15,NOW',
  'projection.dt.interval'      = '1',
  'projection.dt.interval.unit' = 'DAYS',
  'storage.location.template'   = 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/kalshi_universe/dt=${dt}/'
);
```

Verify (read-only, as the pipeline user):

```sql
SELECT dt, count(*) AS markets FROM crypto_raw.kalshi_universe GROUP BY dt ORDER BY dt;
```

## 3. Build + test the dbt models

```bash
cd dbt
DBT_PROFILES_DIR=. uv run dbt build --select +fct_kalshi_opportunity
```

- `stg_kalshi_universe` — view: cast/rename/dedup + derived `mid`, `spread`, `spread_c`,
  and the `is_near_money` (mid ∈ [0.15, 0.85]) / `in_retail_band` (2–15c) / `has_maker_fee`
  flags. One row per `(market_ticker, snapshot_at)`.
- `fct_kalshi_opportunity` — table: one row per `(series_ticker, snapshot_day)`, ranked by
  `spread_capture` (gross half-spread $/day), with median + near-money spread, depth, volume,
  and `has_maker_fee`.

## Notes

- **`spread_capture` is an UPPER BOUND, not P&L** — it assumes you win every fill and ignores
  toxicity. It's a *relative* ranking of where to look; realized edge lives in `fct_lp_daily`.
- **`has_maker_fee`** flags series on Kalshi's Feb-2026 maker-fee list (`fee_type =
  'quadratic_with_maker_fees'`) — a maker fee of `0.0175·C·P·(1−P)`, rounded up per execution,
  can exceed a thin per-fill edge, so treat those series as costly to make.
- **Units**: `spread`/`yes_bid`/`yes_ask` are dollars (0..1); `spread_c` is cents; `volume_24h`
  and `open_interest` are contracts; `liquidity` and `spread_capture` are dollars. Don't mix.
- **Daily last-write-wins**: one file per `dt`. Intraday snapshots would need `snapshot_at` in
  the filename / a finer partition, else the `(market_ticker, snapshot_at)` grain collapses.
- **LIP-eligibility is NOT in the API** — the reward-incentivized-series flag would have to be
  scraped from kalshi.com/incentives or hand-maintained; `fee_type` is the only fee-related
  field the API exposes, and it's captured here.
- **Next**: a Streamlit dashboard over `fct_kalshi_opportunity` (the opportunity radar) — the
  serving layer that also closes the original DE-project spec.
