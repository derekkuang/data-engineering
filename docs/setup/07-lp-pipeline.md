# 07 — LP market-making data pipeline

Turns the live LP bot's local logs into a queryable warehouse layer, on the same
S3 → Glue → Athena → dbt stack as the rest of the platform. This is the data-engineering
layer of the trading bot: ingestion → raw zone → staging → marts → (dashboard / model).

```
core/maker/lp_live.py ──► data/lp_*.csv ──► ingestion/lp_storage.py ──► s3://…/raw/lp_*/dt=…/*.parquet
                                                                      │
                                              Glue external tables (partition projection on dt)
                                                                      │
                                  dbt: stg_lp_* (views) ─► fct_lp_market_session ─► fct_lp_daily
```

## 1. Land the logs to S3

```bash
# needs S3_BUCKET + AWS creds (the same crypto-de-pipeline role used elsewhere)
uv run python -m ingestion.lp_storage           # both datasets
uv run python -m ingestion.lp_storage --sessions-only
```

One Parquet file per `dt` (UTC date of the session), overwritten on re-run, so
re-ingests are idempotent. `dt` lives in the S3 key, not the Parquet columns.

## 2. Create the Glue external tables (one-time, in the Athena console)

The column lists are the contract with `ingestion/lp_storage.py` — keep them in sync.

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS crypto_raw.lp_sessions (
  session_at      timestamp,
  market_ticker   string,
  minutes         double,
  n_fills         bigint,
  fills_per_min   double,
  net_cash        double,
  kalshi_gross    double,
  fees            double,
  max_abs_inv     double,
  pnl_min         double,
  pnl_max         double,
  avg_spread_c    double,
  mean_markout_c  double,
  config_version  string,
  ingested_at     timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/lp_sessions/'
TBLPROPERTIES (
  'projection.enabled'          = 'true',
  'projection.dt.type'          = 'date',
  'projection.dt.format'        = 'yyyy-MM-dd',
  'projection.dt.range'         = '2026-06-01,NOW',
  'projection.dt.interval'      = '1',
  'projection.dt.interval.unit' = 'DAYS',
  'storage.location.template'   = 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/lp_sessions/dt=${dt}/'
);

CREATE EXTERNAL TABLE IF NOT EXISTS crypto_raw.lp_fills (
  session_at      timestamp,
  market_ticker   string,
  fill_at         timestamp,
  book_side       string,
  price           double,
  mid_at_fill     double,
  mid_after       double,
  markout_c       double,
  ingested_at     timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/lp_fills/'
TBLPROPERTIES (
  'projection.enabled'          = 'true',
  'projection.dt.type'          = 'date',
  'projection.dt.format'        = 'yyyy-MM-dd',
  'projection.dt.range'         = '2026-06-01,NOW',
  'projection.dt.interval'      = '1',
  'projection.dt.interval.unit' = 'DAYS',
  'storage.location.template'   = 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/lp_fills/dt=${dt}/'
);
```

## 3. Build + test the dbt models

```bash
cd dbt
DBT_PROFILES_DIR=. uv run dbt build --select +fct_lp_daily
```

- `stg_lp_sessions`, `stg_lp_fills` — views: cast/rename/dedup, no enrichment.
- `fct_lp_market_session` — table: per-session, enriched with ET day + sport + market type
  and the `net = capture + residual − fees` decomposition.
- `fct_lp_daily` — table: one row per ET trading day (net, capture, residual, fill volume,
  fill-weighted markout, per-fill capture) = the OOS tally, queryable.

## Notes

- **Markout vs P&L** live in different grains: markout in `lp_fills`, P&L in `lp_sessions`.
  `fct_lp_daily` aggregates each and joins on `et_day` (fills inherit `et_day` by joining
  back to `fct_lp_market_session`).
- **ET day** (`with_timezone(session_at,'UTC') at time zone 'America/New_York'`) is the
  analysis unit — games and our activity follow US hours; bootstrap CIs resample by day.
- **Next:** a Streamlit dashboard over `fct_lp_daily` / `fct_lp_market_session`, and a
  toxicity/selection model (predict markout from sport/type/activity/spread features).
