# 05 — Kalshi 15-min BTC ingestion (public market data)

Adds a second raw source: **Kalshi `KXBTC15M`** — the "BTC price up in next 15 mins?"
binary market. Its price (0..1 dollars) IS the market-implied probability of an up
move, so it gives us, at 1-minute resolution:

- **implied probability** (`implied_prob_*`) — benchmark + feature
- **yes_bid / yes_ask** — the trading spread = transaction cost (for cost-aware PnL)
- **result** (`yes`/`no`) — the up/down outcome = the directional label

Kalshi market data is **public** (no API key needed); we only read, never trade.
See `ingestion/kalshi.py` (client + normalization), `ingestion/kalshi_storage.py`
(S3 writer), `ingestion/kalshi_backfill.py` (settled-market history), and the live
Airflow task in `airflow/dags/crypto_price_ingest.py`.

## Backfill historical settled markets

```bash
uv run python -m ingestion.kalshi_backfill --days 1 --dry-run   # verify
uv run python -m ingestion.kalshi_backfill --days 7             # land 7 days
```

Lands one Parquet per day at `s3://<bucket>/raw/kalshi_btc_15min/dt=YYYY-MM-DD/candles.parquet`
(idempotent overwrite per day). Paced to respect the public API rate limit.

## Glue external table (run with table-create privileges)

The `crypto-de-pipeline` user is read-only on `crypto_raw` (least privilege), so it
**cannot** create this table — running the DDL as that user fails with
`AccessDenied: glue:CreateTable on database/crypto_raw`. Create it once with
owner/admin access (Athena console, or an admin profile), exactly like
`coinbase_ohlcv` was set up:

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS crypto_raw.kalshi_btc_15min (
  market_ticker      string,
  series_ticker      string,
  window_open_at     timestamp,
  window_close_at    timestamp,
  event_at           timestamp,
  implied_prob_open  double,
  implied_prob_high  double,
  implied_prob_low   double,
  implied_prob_close double,
  implied_prob_mean  double,
  yes_bid_close      double,
  yes_ask_close      double,
  volume             double,
  open_interest      double,
  result             string,
  ingested_at        timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/kalshi_btc_15min/'
TBLPROPERTIES (
  'projection.enabled'           = 'true',
  'projection.dt.type'           = 'date',
  'projection.dt.format'         = 'yyyy-MM-dd',
  'projection.dt.range'          = '2026-01-01,NOW',
  'projection.dt.interval'       = '1',
  'projection.dt.interval.unit'  = 'DAYS',
  'storage.location.template'    = 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/kalshi_btc_15min/dt=${dt}/'
);
```

Partition projection means no crawler / `MSCK REPAIR` — Athena derives the `dt`
partitions from the S3 key pattern, same as `coinbase_ohlcv`.

### Verify (read-only — works as the pipeline user)

```sql
SELECT dt, count(*) AS rows, count(distinct market_ticker) AS markets
FROM crypto_raw.kalshi_btc_15min
GROUP BY dt ORDER BY dt;
```

## Notes

- The `result` column is forward-looking (settlement outcome). It is the **label**
  and must stay OUT of the point-in-time feature mart — joined only at train time.
- The live Airflow task re-fetches the current UTC day and overwrites that day's
  partition (idempotent), so settled markets get their final `result`/candles as
  windows close — same "re-fetch the day, overwrite" contract as `coinbase_ohlcv`.
