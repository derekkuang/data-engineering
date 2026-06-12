# data-engineering

**Project: Crypto Market Data Engineering Platform — and an honest hunt for an edge it was never supposed to find**

---

**Goal**

Build a production-grade ELT platform that ingests crypto market data at minute-to-tick granularity, transforms it into a point-in-time-correct feature mart with dbt, orchestrates it with Airflow + scheduled cloud runs, and stress-tests an ML layer against a real prediction market (Kalshi's 15-minute BTC binaries) with walk-forward validation, real cost modeling, and live-execution experiments. The finished product is something you can walk an interviewer through end-to-end and explain every architectural decision — including why the model, correctly evaluated, makes no money.

**The framing rule.** This is not a Bitcoin price-prediction project. It is a *data platform* project that happens to use crypto data: a point-in-time-correct feature store, incremental loads, a tested transformation layer, and a research harness rigorous enough to kill its own results. The model at the end demonstrates that the platform works — not that markets are beatable. This rule was written before the ML work began, and the ML work proved it right.

---

**Project Status** (updated 2026-06-12) — this table is the source of truth for what exists today.

| Component | Status | Notes |
|---|---|---|
| Coinbase → S3 ingestion | ✅ Built | Paginated, rate-limited, idempotent Parquet writes; BTC + ETH 1-min bars |
| Athena + Glue over S3 raw | ✅ Built | External tables, partition projection, least-privilege IAM, healthchecks |
| dbt staging → intermediate → marts | ✅ Built | `fct_features_pit` (incremental Iceberg/MERGE) + custom point-in-time singular test |
| GitHub Actions CI | ✅ Built | OIDC into a scoped IAM role (no stored keys): `pytest` + `dbt build` (schema + PIT tests) on every push |
| Scheduled cloud pipeline | ✅ Built | GitHub Actions cron: daily Coinbase + Kalshi ingest → `dbt build` — the warehouse stays fresh with no laptop running |
| Airflow `crypto_price_ingest` DAG | ✅ Built | Astro Runtime 3 / Airflow 3, dynamic task mapping; local orchestration demo |
| Kalshi 15-min market ingestion | ✅ Built | `KXBTC15M` implied-prob/spread/result → S3 Parquet; backfill + live DAG task; PIT-safe join into the mart + forward label |
| Derivatives + tick-flow ingestion | ✅ Built | Deribit perp funding (US-accessible); Binance Vision aggTrades archive — **53M trades** aggregated to minute taker-flow features |
| AWS Lambda order-book collector | ✅ Built | Dependency-free handler + CloudFormation (EventBridge cron); banks the **live executable book + BTC spot** at three decision minutes of every 15-min window, 24/7, ~$0/month |
| ML benchmark → model → cost-aware backtest | ✅ Built | Walk-forward only; Kalshi spread + fee cost model; no-skill controls; bootstrap CIs by day |
| Live-execution experiments | ✅ Built | Decision-instant reconciliation, latency gate, forward paper-PnL, decision-minute cluster verdict on live books |
| Unit tests for the research harness | ✅ Built | Money math (fee, PnL/settlement identities) + walk-forward leakage guarantees |
| Streamlit dashboard | ⬜ Next | The visual telling of the arc below |
| `crypto_features_refresh` DAG (run→test→inference) | ⬜ Deprioritized | Honest reason: there is no inference worth scheduling — see the finding |

---

## The Finding — a real platform, and a provably efficient market

The ML layer's job was to answer one question honestly: **using public data, can you beat Kalshi's 15-minute "BTC up or down?" market — net of spread, fees, and execution?** The answer, established over ~10 experiments and two live data-collection campaigns, is **no** — and the way it says no is the strongest part of the project.

The arc (full detail in [docs/devlog.md](docs/devlog.md)):

1. **The market is the benchmark, and it's good.** Kalshi's implied probability scores log loss 0.659 over ~6,100 settled windows and is near-perfectly calibrated (ECE 0.5%). Any model must beat *that*, not a coin flip.
2. **The first "edge" was a leak.** An early model "beat" the market — because the feature set quietly included the market's own price. Excluding all market-derived columns, a walk-forward logistic on pure BTC price/vol features **ties** the market (0.655 vs 0.662). Fancier models don't help: LightGBM is worse and miscalibrated.
3. **A +8% backtest survived five leak-hunts…** The cost-aware backtest (real spreads, Kalshi's fee formula, bet only when the model clears the ask) showed +8% ROI, robust to 3× spread, with every no-skill control losing money.
4. **…so we tested it against reality.** A collector banked the live executable order book at the decision minute. Verdict: the backtest price was real and hittable (slippage unbiased), **but the book reprices ~0.19c/s, tracking BTC spot at R²=0.92** — the edge is a *latency-bound lead-lag artifact* the size of the friction protecting it. Measured end-to-end decision→order latency (~0.6s) is 50× inside the breakeven, but capturing the edge means winning a repricing race against co-located market makers — the wall is competition, not wiring.
5. **Every alternative angle is null, and each null is measured, not assumed:** model class, Deribit funding, Binance order flow (53M trades), favorite-longshot bias, settlement-lag at minute resolution, threshold-ladder arbitrage, options-implied direction (structurally null — options price vol, not 15-min direction), market-making (the fee alone exceeds the half-spread), and less-liquid markets — where the best insight of the project lives: **HYPE's 15-min market is measurably *inefficient* (priced like a coin flip), but its 9c spread scales *with* the inefficiency, trapping it.** Markets are efficient exactly to the limits of arbitrage.
6. **The final test killed the last survivor.** A per-minute sweep had flagged a W+9–13 decision-minute "profit cluster" (~+5–7%, with the honest caveat that day-block bootstrap CIs were ±3–4% and the ranking was split-half unstable). An AWS Lambda collector then banked live books at that minute 24/7 for a week: on **650 fresh out-of-sample windows, the cluster loses −3.7% [−6.5%, −1.4%] at its own assumed prices**, and execution there is a wash (+0.1% paired cost). It wasn't even an execution artifact — it was in-period selection noise, exactly what the uncertainty analysis had warned. The near-expiry book is also 83% empty: nothing to trade against even if a signal existed.

Why this is the portfolio centerpiece rather than a failure: every number above required the platform — PIT-correct features (so the ties are real ties, not leaks), incremental marts (so 70 days × 96 windows × 4 sources stay joinable), serverless collection (the live books are the one irreproducible dataset; the Lambda kept capturing while the laptop slept), and a harness that prices every claim in dollars with controls and day-block confidence intervals. The deliverable is a platform that can tell microstructure from alpha — and a researcher who kills his own result.

---

**The Stack**

- **Ingestion:** Python (httpx, stdlib-only inside Lambda) pulling Coinbase Exchange OHLCV, Kalshi market data (public, read-only), Deribit funding, and Binance Vision aggTrade archives; watermark-driven incremental loads, idempotent day-partition overwrites
- **Collection (serverless):** AWS Lambda + EventBridge cron + CloudFormation — live order-book snapshots at chosen decision minutes, 24/7, dependency-free deploy zip
- **Warehouse:** Amazon Athena (serverless, pay-per-scan) querying S3 Parquet in place via the Glue Data Catalog — a lakehouse, not a load-and-store warehouse
- **Transformation:** dbt Core (`dbt-athena`) — incremental models mandatory at minute granularity; marts materialize as Iceberg tables for clean incremental `MERGE`. This is the centerpiece
- **Orchestration:** Airflow (Astro Runtime 3 / Airflow 3, Docker) locally + GitHub Actions cron for the unattended daily pipeline
- **ML / research:** scikit-learn + LightGBM, walk-forward only, with a cost model (real spreads + Kalshi fee), no-skill controls, day-block bootstrap CIs, and live-execution reconciliation
- **CI:** GitHub Actions via OIDC federation (no stored AWS keys): `pytest` + `dbt build` against the real warehouse on every push
- **Visualization (next):** Streamlit dashboard of the features, the experiments, and the honest arc

---

**The Architecture**

```
Coinbase OHLCV     Kalshi 15-min markets     Deribit funding     Binance aggTrades (53M)
      ↓                    ↓                       ↓                      ↓
            Python ingestion (incremental, watermark-driven, rate-limited)
                                      ↓
        AWS S3 raw zone (Parquet, dt-partitioned)  ←──  AWS Lambda collector
                                      ↓                  (EventBridge 24/7:
        Athena + Glue Data Catalog (queried in place)    live order book + spot
                                      ↓                   at decision minutes)
        dbt: staging → intermediate → marts
            ├── fct_features_pit        — point-in-time feature store (CROWN JEWEL)
            ├── fct_kalshi_15min_label  — forward label, kept OUT of the PIT store
            └── fct_btc_15min_training  — features ⋈ market quote ⋈ label, PIT-safe
                                      ↓
        ml/ research harness
            ├── benchmark_eda · train_baseline · backtest (cost-aware, controls)
            ├── live_exec_reconcile · live_paper_pnl · live_cluster_verdict
            └── settlement_lag · decision_minute_profit · favorite_longshot · …
                                      ↓
        [next] Streamlit dashboard

Orchestration: Airflow DAG (local demo) + GitHub Actions cron (unattended daily
ingest → dbt build) + CI (pytest + dbt build on every push, OIDC, no stored keys)
```

---

**dbt — The Centerpiece**

At minute granularity, dbt stops being "SQL with templates" and starts being a serious data modeling tool.

**Model layers:**
- `staging/` — one model per source table, light cleaning only: type-casts, dedupes on `(asset_id, event_at)`
- `intermediate/` — feature computation per source (returns, rolling realized volatility, RSI, Bollinger positions, Kalshi implied-prob at the decision minute)
- `marts/` — `fct_features_pit` joins sources at minute granularity with point-in-time correctness; `fct_btc_15min_training` adds the market quote and forward label for the ML layer

**Incremental models are mandatory.** Full refreshes on millions of rows are wasteful and slow. Marts use `materialized='incremental'` on Iceberg with `unique_key` MERGE semantics and an `is_incremental()` watermark; backfills stay deterministic via `--full-refresh`.

**Point-in-time correctness — the crown jewel.** `fct_features_pit` guarantees that the row at timestamp T contains only information knowable at T. A custom dbt **singular test** proves it: recompute a sample row's features from raw data using only `event_at <= T` and assert equality (demonstrated to fail under look-ahead). This guarantee is why the ML results above can be trusted — when the model *tied* the market, that was real, and when it "won," the PIT discipline is what exposed the leak.

**Tests:** schema tests (`not_null`, `unique`, `accepted_values`, `relationships`) on every model, the PIT singular test, and physical-sanity singular tests (no negative volumes, high ≥ low, no future timestamps). All run in CI against the real warehouse on every push.

---

**Orchestration — two layers, deliberately**

- **Airflow (`airflow/`, Astro Runtime 3):** the `crypto_price_ingest` DAG demonstrates TaskFlow + dynamic task mapping over the product list, running locally under Docker. It is the orchestration *showcase*.
- **GitHub Actions cron (`.github/workflows/pipeline.yml`):** the unattended *production path* — daily Coinbase + Kalshi ingest followed by `dbt build`, authenticated via OIDC into the same scoped role as CI. Both ingests are idempotent day-overwrites, so reruns and catch-ups are safe by construction. This exists because the honest lesson of running the project was that a laptop is not an orchestrator: the serverless collector kept capturing while the local Airflow stack was off.

The quality gate is preserved in both paths: `dbt build` runs models *and* tests together — a test failure fails the run before anything downstream consumes bad data.

---

**What To Say In An Interview**

*"Walk me through your dbt project structure."*
Staging → intermediate → marts. Staging dedupes and type-casts per source. Intermediate computes features per source. Marts are business-facing: `fct_features_pit` joins price features at minute granularity with point-in-time correctness, and `fct_btc_15min_training` adds the Kalshi quote and the forward label — with the label kept out of the PIT store by design.

*"What does 'point-in-time correct' actually mean?"*
The feature row at timestamp T uses only data with `event_at <= T`. No look-ahead. A custom dbt singular test proves it by recomputing a sample row from raw data and asserting equality. Without this, a time-series model is lying to itself — and in this project the discipline paid for itself by catching a leaked market price that had produced a fake "edge."

*"Why incremental models?"*
At minute granularity a year of data is millions of rows, growing daily. Incremental Iceberg models with MERGE semantics process only new bars; backfills stay deterministic via `--full-refresh`.

*"What happens if a dbt test fails?"*
`dbt build` interleaves run and test, so a failure halts the build before downstream models or any consumer sees bad data — in CI, in the scheduled pipeline, and in the DAG design.

*"How does the model perform?"*
[The honest answer, and the best conversation in the project.] A leakage-free walk-forward logistic *ties* the market's log loss (0.655 vs 0.662). A cost-aware backtest showed +8% ROI that survived five leak-hunts — so I collected live order books and showed it's a latency-bound lead-lag artifact: the book reprices with spot at R²=0.92 before a bet can fill. A second live campaign (AWS Lambda, 650 windows) killed the last candidate edge as in-period selection noise: −3.7% out-of-period at its own assumed prices. Roughly ten angles, all null, each one *measured*. The market is efficient to the limits of arbitrage — I can show exactly where the friction sits, in cents.

*"Why Athena over Snowflake/Redshift?"*
It's a lakehouse: the Parquet in S3 is the single source of truth and Athena queries it in place — no load step, no idle compute. Partition projection prunes scans; pay-per-scan is near-zero at this volume; and the same S3 Parquet stays readable by Spark for the tick-data work (the 53M-trade Binance aggregation). Conscious trade-off: dbt incremental models need Iceberg on Athena for clean MERGE — slightly more setup than Snowflake, documented.

*"What would you do differently in true production?"*
Secrets via Vault, not env files. A feature store (Feast) for low-latency serving. Row-count anomaly detection on each load. Schema contracts. Model monitoring for drift. Separate dev/staging/prod warehouses.

---

**How To Frame It On Your Resume**

> **Only claim what is built and verified.** Everything below exists in this repo today.

- Built an incremental ELT lakehouse on AWS: Python ingestion of four market-data sources (Coinbase OHLCV, Kalshi prediction markets, Deribit funding, Binance tick archives — 53M trades) into an S3 Parquet raw zone, queried in place by Athena via Glue — no load step
- Designed a dbt-athena medallion layer whose feature mart materializes as an **incremental Iceberg table with MERGE upserts**; engineered a point-in-time-correct feature store and **proved it with a custom dbt singular test** (recomputes features from raw using only backward-looking data; demonstrated to fail under look-ahead)
- Stood up **GitHub Actions CI and a scheduled cloud pipeline** (daily ingest → `dbt build`) authenticating via **OIDC federation** into a scoped IAM role — no long-lived AWS keys; orchestrated ingestion with an **Airflow DAG** (Astro Runtime 3, dynamic task mapping)
- Deployed a **dependency-free AWS Lambda collector** (CloudFormation + EventBridge) capturing live order books 24/7 at ~$0/month — the project's one irreproducible dataset
- Built a **walk-forward, cost-aware ML research harness** (real spreads + fees, no-skill controls, day-block bootstrap CIs, unit-tested money math) benchmarked against a real prediction market, and used it plus **two live-execution campaigns** to prove an apparent +8% backtest edge was a latency-bound microstructure artifact — and a candidate decision-minute cluster was selection noise — across ~10 independently tested angles

*Add once built:* Streamlit dashboard.

---

**Repo map**

| Path | What's in it |
|---|---|
| `ingestion/` | Source clients + backfill CLIs (Coinbase, Kalshi, Deribit, Binance Vision) and S3 writers |
| `lambda/orderbook_collector/` | The serverless live-book collector (handler + CloudFormation template) |
| `dbt/` | Staging/intermediate/mart models, schema + PIT tests |
| `airflow/` | Astro project with the `crypto_price_ingest` DAG |
| `ml/` | The research harness — one question per script, shared math in `backtest.py`/`model.py`/`data.py` |
| `tests/` | Unit tests pinning the money math and walk-forward leakage guarantees |
| `scripts/` | Healthchecks, latency measurement, Lambda deploy |
| `docs/devlog.md` | The full day-by-day record: every experiment, result, and dead end |
| `docs/setup/` | One-time infra setup runbooks (Athena, OIDC, Kalshi, Lambda) |
