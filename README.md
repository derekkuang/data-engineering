# data-engineering

**Project: Crypto Market Data Engineering Pipeline**

---

**Goal**

Build a production-grade ELT pipeline that ingests crypto market and on-chain data at minute granularity, transforms it into a point-in-time-correct feature mart using dbt, orchestrates it with Airflow, and surfaces a downstream ML demo (volatility nowcasting or regime classification) plus a Streamlit dashboard. The finished product should be something you can walk an interviewer through end-to-end and explain every architectural decision.

**The framing rule.** This is not a Bitcoin price-prediction project. It is a *data platform* project that happens to use crypto data: a point-in-time-correct feature store, incremental loads, a tested transformation layer, and a backtesting harness. The model at the end is a small demonstration that the platform works — not the point. Read this paragraph again every time you are tempted to over-invest in the model.

---

**Project Status** (updated 2026-06-01) — the rest of this README describes the full target architecture; this table is the source of truth for what actually exists today.

| Component | Status | Notes |
|---|---|---|
| Coinbase → S3 ingestion | ✅ Built | Paginated, rate-limited, idempotent Parquet writes; BTC + ETH 1-min, ~180k rows |
| Athena + Glue over S3 raw | ✅ Built | External table, partition projection, least-privilege IAM, healthcheck |
| dbt staging + intermediate | ✅ Built | `stg_coinbase_ohlcv` (view), `int_price_features` (~25 features, view) |
| dbt mart + PIT test | ✅ Built | `fct_features_pit` (incremental Iceberg/MERGE) + custom point-in-time singular test |
| GitHub Actions CI (OIDC → `dbt build`) | ✅ Built | Green on push — assumes IAM role via OIDC (no stored keys), runs `dbt build` + schema/PIT tests against Athena |
| Airflow `crypto_price_ingest` DAG | ✅ Built | Astro Runtime 3 / Airflow 3, dynamic task mapping; runs locally |
| Kalshi 15-min ingestion (public client + backfill + live DAG task) | ✅ Built | `KXBTC15M` implied-prob/spread/result → S3 Parquet; backfill + live Airflow task; Glue DDL in `docs/setup/05` (run with owner perms) |
| Kalshi → mart join + directional label | ✅ Built | `stg_kalshi_btc_15min` → `int_kalshi_implied_prob` → `kalshi_implied_prob` joined PIT-safe into `fct_features_pit`; `fct_kalshi_15min_label` (forward up/down, out of PIT store) |
| ML model + walk-forward backtest | ⬜ Planned | Kalshi-benchmarked, cost-aware (net of spread/fees) |
| `crypto_features_refresh` DAG (run→test→inference) | ⬜ Planned | The second DAG; quality-gated inference |
| ML model + walk-forward backtest | ⬜ Planned | Kalshi-benchmarked, cost-aware |
| Streamlit dashboard | ⬜ Planned | Features + predictions + PnL |

> **Scope note:** the ML target is now **BTC 15-minute directional**, benchmarked against Kalshi's liquid 15-min binary markets and judged cost-aware (calibration + PnL net of spread/fees) — a deliberate change from the original volatility-nowcasting framing. Some sections below (on-chain Etherscan source, volatility-nowcast Q&A, the four-mart diagram) still describe the *original* plan and are being reconciled.

---

**The Stack**

- **Ingestion:** Python scripts pulling from Coinbase Exchange (price) and Etherscan / mempool.space (on-chain), watermark-driven incremental loads
- **Orchestration:** Apache Airflow — two DAGs (15-minute price ingest, hourly feature refresh)
- **Warehouse:** Amazon Athena (serverless, pay-per-scan), querying S3 Parquet in place via the Glue Data Catalog — a lakehouse, not a load-and-store warehouse
- **Transformation:** dbt Core (`dbt-athena`) — **incremental models are mandatory** at minute granularity; marts materialize as Iceberg tables for clean incremental `MERGE`. This is the centerpiece
- **Storage layer:** AWS S3 as a raw landing zone (Parquet, partitioned by date)
- **ML demo:** lightgbm or sklearn for volatility nowcasting or regime classification, with walk-forward backtest
- **Visualization:** Streamlit dashboard for features + backtested predictions + PnL with realistic costs
- **Infrastructure:** Docker for Airflow (Astronomer Astro CLI), GitHub Actions for CI running dbt tests on every push
- **Version control:** Git throughout, public on GitHub

---

**The Dataset**

Two assets, two-to-three data sources, one year of history.

**Assets:** BTC-USD and ETH-USD. Two pairs is plenty — adding more is volume, not depth.

**Granularity:** 1-minute OHLCV bars. ~525k rows per pair per year. Two pairs ≈ 1M rows of price data, plus on-chain.

**Sources (all free):**

| Layer | Source | What you get | Cadence |
|---|---|---|---|
| Price | Coinbase Exchange API (or Binance public) | OHLCV 1-min bars | Real-time |
| On-chain (BTC) | mempool.space API | Mempool size, avg fee, tx count | ~10 min blocks |
| On-chain (ETH) | Etherscan free API (or Alchemy free tier) | Gas prices, large transfers, active addresses | ~12 sec blocks |
| Derivatives (optional) | Binance futures public API | Funding rates, open interest | 8-hourly |
| Sentiment (optional) | alternative.me Fear & Greed Index | Daily 0-100 score | Daily |

**Recommendation for v1: price + one on-chain source.** Two sources is enough to demonstrate multi-source time-joins; adding more is scope creep before the foundations are tested. Add the third source in v1.1 once the pipeline is green.

---

**The Architecture**

```
Coinbase API (price) + Etherscan API (on-chain)
        ↓
Python Ingestion (incremental, watermark-driven, rate-limit-aware)
        ↓
AWS S3 (raw landing — Parquet, partitioned by date) — single source of truth
        ↓
Athena + Glue Data Catalog (external table over S3 — queried in place, no load step)
        ↓
dbt (transformation layer — all marts incremental, materialized as Iceberg)
    ├── Staging models       — type-cast, dedupe on (asset_id, event_at)
    ├── Intermediate models  — compute features per source
    └── Mart models:
            ├── fct_minute_bars        — OHLCV fact table
            ├── fct_features_pit       — point-in-time feature store (CROWN JEWEL)
            ├── fct_model_predictions  — model output written back
            └── dim_assets             — asset dimension
        ↓
Airflow DAGs:
    ├── crypto_price_ingest      — every 15 min, incremental load
    └── crypto_features_refresh  — hourly: dbt run → dbt test → model inference
        ↓
Streamlit Dashboard (features + backtested predictions + PnL with realistic costs)
```

---

**dbt — The Centerpiece**

This is what interviewers will actually ask about. At minute granularity, dbt stops being "SQL with templates" and starts being a serious data modeling tool.

**Model layers:**
- `staging/` — one model per source table, light cleaning only. Example: `stg_coinbase_ohlcv.sql` type-casts timestamps, dedupes on `(asset_id, event_at)`, drops bars with null close.
- `intermediate/` — feature computation per source. Example: `int_price_features.sql` computes 5/15/60-minute returns, rolling realized volatility, RSI, Bollinger band positions.
- `marts/` — final business-facing tables. Example: `fct_features_pit.sql` joins price + on-chain features at minute granularity with point-in-time correctness.

**Incremental models are mandatory.** Full refreshes on millions of rows are wasteful and slow. Use `materialized='incremental'` with `unique_key=['asset_id', 'event_at']` and an `is_incremental()` filter that processes only new bars since the last run. Backfills remain deterministic via `dbt run --full-refresh`.

**Point-in-time correctness — the crown jewel.** `fct_features_pit` must guarantee that the row at timestamp T contains only information knowable at T. No look-ahead. No peeking. This is the single biggest concept that separates ML-ready feature engineering from "I shuffled my time series and got 95% accuracy."

**Tests:**
- Schema tests for every model: `not_null`, `unique`, `accepted_values`, `relationships`
- **A custom singular test that proves point-in-time correctness:** pick a row at time T, recompute its features using only `event_at <= T`, assert they match. *This one test, mentioned in your README, is worth more than any model accuracy number.*
- Additional singular tests: no negative volumes, high ≥ low, no future timestamps
- `dbt test` runs in GitHub Actions on every push

**Documentation:**
- `description:` fields for every model and every column in `schema.yml`
- Run `dbt docs generate` and `dbt docs serve` — screenshot for the README
- Embed the dbt-docs DAG screenshot in the README

**A sample incremental staging model:**
```sql
-- models/staging/stg_coinbase_ohlcv.sql
{{ config(
    materialized='incremental',
    unique_key=['asset_id', 'event_at']
) }}

with source as (
    select * from {{ source('raw', 'coinbase_ohlcv') }}
    {% if is_incremental() %}
      where event_at > (select coalesce(max(event_at), '2000-01-01') from {{ this }})
    {% endif %}
),
cleaned as (
    select
        product_id                  as asset_id,
        time::timestamp             as event_at,
        open::numeric(18,8)         as open_price,
        high::numeric(18,8)         as high_price,
        low::numeric(18,8)          as low_price,
        close::numeric(18,8)        as close_price,
        volume::numeric(28,8)       as volume
    from source
    where close is not null
      and volume >= 0
      and high >= low
)
select * from cleaned
```

---

**Airflow — The Orchestration Layer**

Run Airflow locally with Docker (Astronomer Astro CLI). The project lives in `airflow/` (Astro Runtime 3 / Airflow 3); `astro dev start` brings up the stack.

> **Status:** DAG 1 (`crypto_price_ingest`) is **built and runs** — see `airflow/dags/crypto_price_ingest.py`. It uses TaskFlow + dynamic task mapping over the product list. DAG 2 (`crypto_features_refresh`) is **planned**. The description below is the target design for both.

**Two DAGs, not one.** Crypto data moves faster than the daily-batch pattern assumes, so split ingestion from transformation:

**DAG 1: `crypto_price_ingest`** — runs every 15 minutes
1. `fetch_new_bars` — Python operator pulling new bars from Coinbase since last watermark
2. `upload_to_s3` — write Parquet to `s3://.../raw/coinbase_ohlcv/dt=YYYY-MM-DD/`
3. `update_watermark` — record the latest ingested timestamp

   (No load step — Athena queries the new Parquet in place via partition projection. The lakehouse pays off here: landing the file *is* loading it.)

**DAG 2: `crypto_features_refresh`** — runs hourly
1. `dbt_run_staging` — `dbt run --select staging`
2. `dbt_run_intermediate` — `dbt run --select intermediate`
3. `dbt_run_marts` — `dbt run --select marts`
4. `dbt_test` — `dbt test`; **failure halts downstream tasks**
5. `run_inference` — load `fct_features_pit`, run model, write predictions to `fct_model_predictions`
6. `notify_on_failure` — Slack/email alert

The `dbt_test >> run_inference` dependency is the single most important Airflow design decision in this project: it gates model inference on data quality. Bad data never reaches the model.

**A sample DAG skeleton:**
```python
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
from ingestion.coinbase import fetch_new_bars

default_args = {
    'owner': 'derek',
    'retries': 2,
    'retry_delay': timedelta(minutes=5),
    'email_on_failure': True,
}

with DAG(
    dag_id='crypto_price_ingest',
    default_args=default_args,
    schedule_interval='*/15 * * * *',
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=['crypto', 'ingest'],
) as dag:

    fetch = PythonOperator(
        task_id='fetch_new_bars',
        python_callable=fetch_new_bars,
    )

    upload = PythonOperator(
        task_id='upload_to_s3',
        python_callable=upload_parquet_to_s3,
    )

    fetch >> upload  # no warehouse-load task — Athena reads the S3 Parquet directly
```

---

**Getting Started — Step By Step**

**Week 1: Infrastructure and raw ingestion**
1. Write the Coinbase ingestion script — pull 2 months of BTC-USD and ETH-USD 1-min bars, upload Parquet to `s3://.../raw/coinbase_ohlcv/dt=YYYY-MM-DD/`
2. **Implement incremental loads with a watermark** — the second run must only fetch new bars
3. Set up Athena over the S3 raw zone (Glue external table + partition projection, workgroup, healthcheck) — see `docs/setup/03-athena-s3.md`
4. Confirm raw data is queryable in Athena and the incremental contract holds
5. Install Astro CLI, `astro dev init`

**Week 2: dbt transformation layer (the heart of the project)**
1. `uv add dbt-athena-community`, then `dbt init` (profile: `type: athena`, workgroup `crypto_wg`, region `us-east-1`)
2. Write `stg_coinbase_ohlcv.sql` as an incremental model
3. Write `int_price_features.sql` (returns, rolling realized volatility, RSI)
4. Add the on-chain source — write `stg_etherscan_gas.sql` and `int_onchain_features.sql`
5. Write `fct_features_pit.sql` — the point-in-time feature mart
6. **Write the PIT-correctness singular test** (non-negotiable; this is the project's signature)
7. Add schema tests for every model
8. `dbt docs generate` — screenshot for the README

**Week 3: Orchestration, ML demo, CI, polish**
1. Wire the two Airflow DAGs
2. Train a simple model (lightgbm) on `fct_features_pit`. Target options:
   - **Realized volatility nowcast** (recommended — models genuinely work)
   - **Regime classification** (high-vol vs low-vol)
   - Directional prediction — only if reported honestly with walk-forward results and transaction costs
3. Walk-forward validation only — no shuffled splits, no data leakage
4. Write predictions back to `fct_model_predictions` from an Airflow task
5. Set up GitHub Actions to run `dbt test` on every push (use dbt's official action)
6. Build a Streamlit dashboard: features over time, predictions, backtest PnL with 5bps transaction costs
7. Write the README sections explaining architecture and *why* PIT correctness matters
8. Record a 2-minute Loom walkthrough; link in README

---

**What To Say In An Interview**

*"Walk me through your dbt project structure."*
Staging → intermediate → marts. Staging dedupes and type-casts per source table. Intermediate computes features per source. Marts are business-facing; `fct_features_pit` joins price + on-chain features at minute granularity with point-in-time correctness.

*"What does 'point-in-time correct' actually mean?"*
The feature row at timestamp T uses only data with `event_at <= T`. No look-ahead. I have a custom dbt singular test that proves it by recomputing a sample row from raw data and asserting equality. Without this guarantee, a time-series ML model trained on the features is lying to itself.

*"Why incremental models?"*
At minute granularity, a year of two assets is ~1M rows of price data alone, growing daily. Full refreshes are wasteful and slow. Incremental models with `unique_key=(asset_id, event_at)` and an `is_incremental()` watermark process only new bars each run. Backfills are still deterministic via `--full-refresh`.

*"What happens if a dbt test fails?"*
The features_refresh DAG halts at `dbt_test` and never reaches `run_inference`. The mart is not updated and the model is not run on bad data. Quality gating downstream of tests is the whole point of the orchestration design.

*"How does the model perform?"*
[Honest answer.] For directional prediction, ~52% accuracy out-of-sample, not profitable after 5bps transaction costs. The volatility nowcasting model performs meaningfully better — but the point of this project is the platform, not the alpha. Production ML is mostly about the pipeline feeding the model.

*"Why Athena over Snowflake/Redshift?"*
It's a lakehouse, not a warehouse: the Parquet in S3 is the single source of truth and Athena queries it in place — no load step, no cluster to manage, no compute running when idle. Pay-per-scan is near-zero at this data volume, and partition projection prunes scans to the `dt` partitions a query needs. It also keeps the stack single-cloud (no cross-cloud egress) and Phase-3-ready: the same S3 Parquet is readable by Spark (Glue/EMR) for tick-data aggregation. Trade-off I made consciously: dbt incremental models need Iceberg table format on Athena to get clean `MERGE` semantics — slightly more setup than Snowflake, which I document.

*"What would you do differently in true production?"*
Secrets via Vault, not env files. Feature store (Feast) instead of a dbt mart for low-latency serving. Row-count anomaly detection on each load. Schema contracts via dbt 1.5+. Model monitoring for feature drift. Separate dev/staging/prod warehouses.

---

**How To Frame It On Your Resume**

> **Only claim what is built and verified.** The bullets below describe the code
> that exists in this repo today (see the Project Status table near the top).
> Do not add the dashboard / model / second DAG bullets until those are shipped.

*Accurate today:*

- Built an incremental ELT lakehouse on AWS: Python ingestion of minute-granularity Coinbase OHLCV into an S3 Parquet raw zone (idempotent, partition-projected), queried in place by Athena via the Glue Data Catalog — no load step
- Designed a dbt-athena medallion layer (staging → intermediate → marts) whose feature mart materializes as an **incremental Iceberg table with MERGE upserts**; engineered a point-in-time-correct feature store (~25 price/volatility/momentum features) and **proved it with a custom dbt singular test** that recomputes a feature from raw using only backward-looking data and asserts equality (demonstrated to fail under look-ahead)
- Stood up **GitHub Actions CI running `dbt build`** (model run + schema/PIT tests) against Athena on every push, authenticating via **GitHub OIDC federation** into a scoped IAM role — no long-lived AWS keys stored; orchestrated ingestion with an **Airflow DAG** (Astro Runtime 3 / Airflow 3, dynamic task mapping) run locally

*Add once built (currently planned — see Project Status):* hourly `dbt run → test → inference` feature-refresh DAG; Kalshi-benchmarked directional model with walk-forward backtest net of costs; Streamlit dashboard.

The accurate bullets already demonstrate: incremental dbt, point-in-time correctness, Iceberg/lakehouse modeling, OIDC-secured CI for data, and API ingestion. That is the core of the DE skill stack — kept honest.
