# data-engineering

**A market-data platform that hunts trading edges on Kalshi — and is rigorous enough to kill its own results.**

It began as a crypto ELT portfolio project: ingest market data, model it with dbt, prove the pipeline with an ML layer benchmarked against a real prediction market. The ML layer did its job *by proving the market efficient* — and the platform outgrew its first target. Today it is an end-to-end research-and-trading system for Kalshi: a daily whole-exchange opportunity radar, an in-play microstructure capture that measures which order flow is safe to trade against, a live real-money market-making bot, and a statistical verdict layer that decides — with day-block confidence intervals — whether the one surviving edge is real.

**The framing rule, written before any model was trained:** this is a *data platform* project, not a prediction project. The platform's job is to produce answers trustworthy enough to bet real money on — including, especially, the answer "no." That rule has now survived two acts: the crypto act ended with a provably efficient market, and the trading act is being decided by measurement rather than hope.

---

**Project Status** (updated 2026-07-22) — this table is the source of truth for what exists today.

| Layer | Component | Status | Notes |
|---|---|---|---|
| Ingestion | Coinbase / Kalshi / Deribit / Binance history | ✅ Built | 4 sources; watermark-driven, idempotent day-partition Parquet; 53M-trade tick aggregation |
| Ingestion | **Kalshi universe snapshot** (whole exchange, daily) | ✅ **Live** | ~12,000 open markets / ~1,800 series per day: spread, depth, volume, maker-fee flag |
| Ingestion | **In-play WS microstructure capture** | ✅ Live | Multi-market WebSocket book+flow features every ~5s during games, scheduled at game windows |
| Ingestion | AWS Lambda order-book collector | ✅ Built | Serverless 24/7 capture of live books at decision minutes (~$0/mo); the crypto act's irreproducible dataset |
| Warehouse | S3 → Glue → Athena lakehouse | ✅ Live | 6 external tables, partition projection, least-privilege IAM |
| Warehouse | dbt medallion (16 models, ~80 tests) | ✅ Live | PIT-correct feature store (Iceberg MERGE) + LP P&L marts + opportunity mart + markout/toxicity marts |
| Serving | **Streamlit "Opportunity Radar" dashboard** | ✅ Built | Spread-vs-volume landscape, maker-fee flags, sweet-spot table; offline-first, deploys with no AWS creds |
| Execution | **Live market-making bot** (real money) | ✅ Built | Two-sided quoting, inventory skew, kill switches, WS-fed book + fills, per-fill markout logging |
| Measurement | **Toxicity scoreboard + edge verdict** | ✅ Built | Flow-signed markout per family/day → day-block bootstrap CIs → FLOW-TOXIC / BENIGN / INSUFFICIENT |
| Orchestration | CI + daily pipeline + game-window capture (GitHub Actions, OIDC) | ✅ Live | `pytest` + `dbt build` against the real warehouse on every push; two scheduled data crons; no stored AWS keys |
| Orchestration | Airflow DAG (Astro Runtime 3) | ✅ Built | Local orchestration showcase; Actions is the unattended production path |
| Research | The edge question itself | 🔄 **Measuring** | The machine is collecting; the verdict script decides when ~5+ game days accrue |

---

## The story, in three acts

Full day-by-day record: [docs/devlog.md](docs/devlog.md) (24 entries — every experiment, number, and dead end).

### Act 1 — The crypto platform, and a provably efficient market

The original question: **using public data, can you beat Kalshi's 15-minute "BTC up or down?" market — net of spread, fees, and execution?** Answer, over ~10 experiments and two live data-collection campaigns: **no** — and the way the platform says no is the point.

1. **The market is the benchmark, and it's good**: log loss 0.659 over ~6,100 settled windows, near-perfectly calibrated (ECE 0.5%).
2. **The first "edge" was a leak** — caught by the point-in-time discipline. Leak-free, a walk-forward logistic **ties** the market (0.655 vs 0.662).
3. **A +8% cost-aware backtest survived five leak-hunts** — so it was tested against reality: live order books captured at the decision instant showed the book repricing with BTC spot at R²=0.92 before an order could fill. A latency-bound lead-lag artifact, not alpha.
4. **The last candidate (a decision-minute "profit cluster") was killed out-of-sample** by a week of serverless collection: −3.7% [−6.5, −1.4] on 650 fresh windows at its own assumed prices. In-period selection noise — exactly what its own day-block CIs had warned.
5. Best single insight: less-liquid markets (HYPE) are measurably *mispriced* — and untradeable, because **the spread scales with the inefficiency**. Markets are efficient exactly to the limits of arbitrage.

### Act 2 — The edge hunt: ~17 hypotheses, each measured, each killed

The platform then swept every adjacent edge hypothesis. Every verdict below is *measured* (with method), not assumed:

| Hypothesis | Verdict | How it was decided |
|---|---|---|
| BTC direction models (LR/GBM, walk-forward) | Null | Tie the market at best; leak-free by PIT construction |
| +8% lead-lag backtest | Artifact | Live decision-instant books: repricing race, R²=0.92 vs spot |
| Decision-minute cluster (W+9–13) | Selection noise | 650 fresh OOS windows via Lambda: −3.7% at own prices |
| Favorite-longshot bias | Untradeable | Persistent, but trapped inside spread at every depth |
| Settlement lag / threshold arb / options-implied | Null | Minute + live-book resolution; options price vol, not 15-min direction |
| ETH / HYPE (thinner markets) | Untradeable | Friction scales with inefficiency (9c spread on the mispriced one) |
| BTC market-making | Null | Sub-minute adverse selection; breakeven repricing ~3s |
| Tennis (MM + directional) | Null | In-play martingale; autocorr ≈ 0, order-flow ≈ 0 |
| Polymarket (spread + reward farming) | Dead | 1c spreads; rewards cover 2.2% of goal pick-off, measured live |
| Cross-venue arb, binary↔perp basis | Null | Only offshore legs diverge; US-legal legs don't |
| Kalshi weather — making | Dead | Maker pays ~0.44c/fill into end-of-day convergence pick-off |
| Kalshi weather — forecasting | Dead | The crowd beats an NWP model at every morning decision point |
| Kalshi perpetuals (funding arb) | Closed | ~$0.10 per $1k per settlement at retail capital; API gated |
| **In-play soccer market-making** | **Survivor — unconfirmed** | The only positive cell; see Act 3 |

### Act 3 — The survivor, and the machine built to judge it

The one strategy that made real money: **two-sided market-making in Kalshi's in-play soccer totals/spreads** — rare, discrete scoring keeps those books mean-reverting, so a maker collects wide retail spreads without being run over. A live bot (staged safety ladder, inventory skew, kill switches, per-fill markout logging) traded it with real money: **9 World-Cup days, net +$70, +0.59c/fill captured, 7/9 days positive** — and, honestly: thin, seasonal, fading as the tournament ended, and **still unconfirmed** as durable edge. External evidence agrees with the internal: an academic study of Kalshi's own 2021–2025 data finds makers-on-favorites is the exchange's only systematically positive cell.

So the project became the machine that decides the question properly:

- **The opportunity radar** (`fct_kalshi_opportunity` + the dashboard): every series daily, ranked by gross capturable spread, flagged for maker fees — *where could an edge live?* (Kalshi's own Pro terminal ships a real-time screener; this platform's edge is the **historical/analytical** view it can't provide.)
- **The toxicity capture** (`fct_ws_markout` → `fct_toxicity_by_family`): in-play flow measured every 5s, each snapshot labeled with what the price did 30s later. Net-taker-flow-predicts-price = a maker gets picked off. Known-toxic families (ITF tennis, MLB games) are captured **deliberately as instrument controls** — if they don't read toxic, the tool is broken, not the market benign.
- **The verdict** (`core/maker/edge_verdict.py`): per family, day-block bootstrap CIs, split-half stability, multiple-comparison caveats → `FLOW-TOXIC / FLOW-BENIGN / INCONCLUSIVE / INSUFFICIENT`. Benign flow gates real capital; realized capture on our own fills — same discipline — delivers the final answer.

**Current status: the machine is collecting.** Either outcome completes the project honestly — a confirmed edge gets scaled onto the multi-market WebSocket infrastructure already built for it; a null gets written up like the seventeen before it.

---

**The Architecture**

```
 Coinbase · Kalshi history · Deribit · Binance (53M)      Kalshi UNIVERSE (daily)      In-play WS capture (game windows)
                    ↓                                            ↓                            ↓
              Python ingestion (incremental, idempotent day-partition Parquet)   ←   AWS Lambda collector (24/7)
                                                     ↓
                         S3 raw zone  →  Glue Data Catalog  →  Athena (queried in place)
                                                     ↓
                 dbt: 6 staging → 2 intermediate → 8 marts   (~80 tests, PIT singular test)
                     ├── fct_features_pit          — point-in-time feature store (Iceberg MERGE)
                     ├── fct_kalshi_opportunity    — the whole-exchange opportunity radar
                     ├── fct_ws_markout            — flow → 30s-forward price label (toxicity)
                     ├── fct_toxicity_by_family    — the per-family/day toxicity scoreboard
                     └── fct_lp_market_session/daily — the bot's real-money P&L, decomposed
                              ↓                                   ↓
              Streamlit Opportunity Radar             core/maker/edge_verdict.py  (day-block CIs)
                              ↓                                   ↓
                    core/maker/lp_live.py — the live maker (REST + WebSocket book/fills, kill switches)

 Orchestration: GitHub Actions (CI on push · daily pipeline cron · game-window capture cron; OIDC, no stored keys)
                + Airflow DAG (local showcase) + Lambda/EventBridge (serverless collection)
```

---

**dbt — the centerpiece**

- `staging/` (6) — one view per raw source: type-casts, dedupes, derived quote fields
- `intermediate/` (2) — per-source feature computation
- `marts/` (8) — the business layer: the PIT feature store (incremental Iceberg MERGE), the training mart, the opportunity radar, the markout/toxicity pair, and the live bot's P&L decomposition (`net = capture + residual − fees`)

**Point-in-time correctness is the crown jewel** — `fct_features_pit` guarantees row-at-T uses only data knowable at T, *proven* by a custom singular test that recomputes features from raw backward-looking data and asserts equality. This discipline caught the leaked market price behind the project's first fake "edge," and it is why every tie and every null above can be trusted. ~80 schema/range/uniqueness tests run against the real warehouse in CI on every push.

---

**Orchestration — deliberately layered**

- **GitHub Actions** is the production path: CI (`pytest` + full `dbt build` vs Athena on every push), the daily 02:30 UTC pipeline (all ingests + `dbt build`), and the game-window capture cron (19/23/02 UTC; re-exchanges its OIDC token before landing because captures outlive the 1-hour session). All via OIDC federation — no stored AWS keys.
- **Airflow** (Astro Runtime 3) remains as the orchestration showcase; the honest lesson stands: a laptop is not an orchestrator, and the serverless paths kept collecting while it slept.

---

**What to say in an interview**

*"Walk me through the platform."*
Four sources plus two live capture systems land idempotent Parquet in S3; Athena queries it in place through Glue; dbt models it in three layers with ~80 tests including a custom point-in-time proof; GitHub Actions runs CI against the real warehouse and two scheduled data pipelines with OIDC; a Streamlit dashboard serves the ranked output; and a live trading bot both consumes the answers and generates the ground-truth P&L data that feeds back in.

*"What does 'point-in-time correct' mean and why care?"*
The feature row at T uses only `event_at <= T`, proven by a singular test that fails under look-ahead. It caught a leaked market price that had produced a fake edge — the difference between a model that ties the market and one that lies to itself.

*"How did the ML perform?"*
The honest answer is the best part: a leak-free walk-forward model *ties* the market. A +8% backtest survived five leak-hunts, so I captured live order books and proved it was a latency artifact — the book reprices with spot at R²=0.92 before an order fills. ~17 hypotheses total, each measured to a verdict. The market is efficient to the limits of arbitrage, and I can show where the friction sits, in cents.

*"So why market-making, and how do you know if that edge is real?"*
Because every *taking* strategy was null, and the one structurally different role — providing liquidity into mean-reverting retail books — made real money in a live test. Whether it's durable is being decided by a measurement funnel: a daily whole-exchange screen finds where spreads and volume live; an in-play capture labels each market's flow with 30-second-forward markout (with known-toxic families captured as instrument controls); and a verdict script applies day-block bootstrap CIs — days are the resampling unit, not fills — before any capital scales. Benign flow is necessary, not sufficient: the final judge is realized capture on our own fills.

*"Why Athena over Snowflake?"*
Lakehouse: S3 Parquet is the single source of truth, queried in place, pay-per-scan, near-zero idle cost at this volume; Iceberg gives dbt clean incremental MERGE. Trade-off documented and conscious.

---

**Resume framing** (everything below exists in this repo today)

- Built an incremental ELT lakehouse on AWS: 6 market-data pipelines (4 historical sources incl. a 53M-trade tick aggregation + a daily whole-exchange snapshot + a live WebSocket microstructure capture) landing idempotent Parquet in S3, queried in place by Athena/Glue
- Designed a 16-model dbt medallion with an **incremental Iceberg MERGE feature store proven point-in-time-correct by a custom singular test**; ~80 tests run against the production warehouse in CI on every push
- Stood up **three GitHub Actions pipelines (CI, daily ELT, scheduled capture) via OIDC federation** — no long-lived AWS keys — plus an Airflow DAG and a dependency-free AWS Lambda/EventBridge collector (~$0/mo)
- Built and operated a **live market-making bot (real money)** on Kalshi with staged safety controls, WebSocket order-book/fill feeds, and per-fill markout logging feeding the warehouse
- Ran a **walk-forward, cost-aware research program** (~17 edge hypotheses; day-block bootstrap CIs, no-skill controls, live-execution reconciliation) that correctly killed every false edge — including a +8% backtest exposed as a latency artifact via live order-book capture — and productionized the measurement funnel (opportunity radar → flow-toxicity labeling with instrument controls → statistical verdict) now deciding the surviving one
- Shipped the serving layer: a **Streamlit opportunity-radar dashboard** over the dbt marts, offline-first and deployable without warehouse credentials

---

**Repo map**

| Path | What's in it |
|---|---|
| `ingestion/` | Source clients + backfill CLIs + S3 writers (incl. `kalshi_universe`, `ws_feature_storage`, `lp_storage`) |
| `lambda/orderbook_collector/` | Serverless live-book collector (handler + CloudFormation) |
| `dbt/` | 16 models in 3 layers, schema + PIT tests |
| `dashboard/` | The Streamlit Opportunity Radar (`app.py`) + Athena snapshot publisher |
| `airflow/` | Astro project, `crypto_price_ingest` DAG |
| `core/` | Reusable engines: `maker/` (live bot, gates, verdicts), `capture/` (WS tape/book), `backtest/` (walk-forward harness) |
| `strategies/` | **The edge map** — one folder per strategy with its `VERDICT.md`: `soccer_mm/` (ACTIVE), `politics_mm/` (gated), `btc_direction/`, `weather_taker/`, `tennis/`, `polymarket/`, `cross_venue/`, `parlays/` (closed) |
| `tests/` | 54 unit tests: money math, walk-forward leakage, storage contracts, feed parsing |
| `scripts/` | Healthchecks, latency measurement, Lambda deploy |
| `docs/devlog.md` | The full record — 24 entries, every experiment and dead end |
| `docs/setup/` | 10 infra runbooks (Athena, OIDC, Lambda, LP pipeline, universe snapshot, WS capture) |

**Run the dashboard:** `uv sync --group dashboard && uv run --group dashboard streamlit run dashboard/app.py`
