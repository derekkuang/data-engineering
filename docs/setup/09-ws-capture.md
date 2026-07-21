# 09 — WS in-play capture (toxicity / ML feature store)

The auto-logger: capture the live in-play microstructure of the active sports board during
games, land it in the warehouse, and label it with forward markout — the data to (a) answer
"is this market's flow toxic?" (the edge test) and (b) train the toxicity/selection model.

```
ml/lp/ws_features.py ──► data/ws_features.csv ──► ingestion/ws_feature_storage.py ──► s3://…/raw/ws_features/dt=…/features.parquet
   (scheduled by .github/workflows/ws-capture.yml)                                          │
                                                        Glue external table (partition projection on dt)
                                                                                            │
                                       dbt: stg_ws_features (view) ──► fct_ws_markout (table, the label)
```

`ws_features` selects the relevant markets itself (active, gate-eligible TOTAL/SPREAD sports
markets, prefix-filterable — the same universe the maker quotes) and rolls as games start/end.
It places no orders and needs no trading key.

## 1. Capture + land (automatic)

`.github/workflows/ws-capture.yml` fires at game-heavy windows (19:00 / 23:00 / 02:00 UTC),
captures ~90 min of the live board, and lands it to S3.

**One-time: add the Kalshi key as repo secrets.** Kalshi's WebSocket requires auth even for
market data, so the workflow needs `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY` (the PEM file's
*contents*) as GitHub Actions repo secrets (Settings → Secrets and variables → Actions).
**Risk note:** Kalshi API keys are full-account — there is no read-only scope — so the secret
could place orders if exfiltrated. Mitigations: Actions secrets are encrypted, never exposed to
fork PRs, and this is a single-maintainer repo; for stricter isolation, create a SEPARATE Kalshi
API key for CI (revocable independently of the trading key), or run the capture on AWS with the
key in Secrets Manager instead (see Notes).

To run a specific game by hand, use
the workflow's `workflow_dispatch` (set `minutes` / `prefix`), or locally:

```bash
uv run python -m ml.lp.ws_features --prefix KXWC --minutes 90   # capture -> data/ws_features.csv
uv run python -m ingestion.ws_feature_storage                    # land -> S3 (or --local-dir ./wh)
```

## 2. Create the Glue external table (one-time, admin identity)

Column list = the contract with `ingestion/ws_feature_storage.py`'s `FEATURES_SCHEMA`.

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS crypto_raw.ws_features (
  snapshot_at     timestamp,
  market_ticker   string,
  bid             double,
  ask             double,
  mid             double,
  spread_c        double,
  imbalance       double,
  yes_depth       double,
  no_depth        double,
  depth_near      double,
  n_levels        double,
  trades_1m       double,
  vol_1m          double,
  taker_buy_frac  double,
  signed_flow_1m  double,
  midvol_1m       double,
  midmove_1m      double,
  ingested_at     timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/ws_features/'
TBLPROPERTIES (
  'projection.enabled'          = 'true',
  'projection.dt.type'          = 'date',
  'projection.dt.format'        = 'yyyy-MM-dd',
  'projection.dt.range'         = '2026-07-18,NOW',
  'projection.dt.interval'      = '1',
  'projection.dt.interval.unit' = 'DAYS',
  'storage.location.template'   = 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/ws_features/dt=${dt}/'
);
```

Run it under the **admin** profile before the nightly `dbt build` first sees the new models,
or that job fails on a missing source (same ordering as docs/setup/08).

## 3. Build the models

The nightly `scheduled pipeline` builds these once the table exists; or manually:

```bash
cd dbt && DBT_PROFILES_DIR=. uv run dbt build --select +fct_ws_markout
```

- `stg_ws_features` — view: cleaned per-(market, snapshot) features. The model feature set.
- `fct_ws_markout` — table: each snapshot joined forward (H=30s) to a later mid.
  `flow_signed_markout_c` > 0 = net taker flow predicted the move = **toxic** (a resting maker
  gets picked off). Aggregate per market/type to rank toxicity; use the microstructure columns
  as features to train the selection model.

## Notes

- **The markout label is self-contained** — it needs no settlement data; the forward mid comes
  from continued logging within the same capture. Validate the H=30s / 120s-window join on real
  data once a few games have accumulated; tune the horizon in `fct_ws_markout.sql`.
- **Coarse schedule**: the fixed crons are a net over global game clusters, not per-game
  precision — the fixed-cron trade-off vs a standing daemon. Add `workflow_dispatch` runs for
  specific games, or move to an always-on runner (Phase 6) for full coverage.
- **This is the edge test + the ML input**: the toxicity question ("is the flow benign?") the
  WC result left open is answered by aggregating `flow_signed_markout_c` per market here.
