# Development Log

A running journal of work on the crypto data-engineering pipeline — what I did, why, and what I learned each day, so I can refer back and explain decisions in interviews. **Newest entries at the top.**

---

## 2026-07-20 — CI green: created the missing Glue tables + landed LP data (the flagged merge prerequisite)

`ci.yml` runs `dbt build` against Athena on every push, and it was failing: `Table 'awsdatacatalog.crypto_raw.lp_fills'/'lp_sessions' does not exist` (the lp — and, on newer commits, ws_features — Glue tables were never created; the models select from them). This is exactly the prerequisite flagged in the 07-16→18 entry. Fixed properly (not patched): landed the real LP bot CSVs to S3 (`ingestion.lp_storage`, June 16-27, 12 partitions each), created `lp_sessions`+`lp_fills`+`ws_features` Glue tables (admin profile, DDL from docs/07+09), and ran the full `dbt build` vs Athena = **PASS=94, ERROR=0, SKIP=0**. `fct_lp_daily`/`fct_lp_market_session` are now LIVE with real data; `ws_features`/`fct_ws_markout` exist but empty until the auto-logger runs. Merge to main is now safe. Root coupling to remember: CI builds ALL models against the real warehouse on push, so create a new model's Glue source table BEFORE pushing the model.

---

## 2026-07-16 → 18 — universe mart LIVE in Athena; opportunity dashboard + auto-logger built; --ws maker bug fixed

Built out the "full Kalshi platform" from the 07-15 mart into a working end-to-end slice + serving layer + collection loop. All committed on `ws-multimarket-logger` (pushed to origin, tip `998a718`); NOT merged to main.

**AWS promotion — the universe mart is LIVE.** Landed a real snapshot to S3, created the Glue table `crypto_raw.kalshi_universe` (admin profile = `crypto-de-deployer`; pipeline user `crypto-de-pipeline` is read-only), verified read-only, and `dbt build --select +fct_kalshi_opportunity` against Athena = **PASS=13** (stg view + mart table + 11 tests). Real data: **12,080 open markets / 1,795 series**; the radar's sweet-spot correctly surfaces the soccer/sports totals (KXWCTOTALGOAL, KXITF, etc.) and flags the maker-fee traps. Added `.env`-driven daily snapshot step to `pipeline.yml`.

**Dashboard (component 4) — the serving layer, committed.** `dashboard/app.py` (Streamlit "Kalshi Opportunity Radar": KPI tiles, spread-vs-volume scatter w/ maker-fee color + retail-band shading, category bar, sweet-spot table), `publish_snapshot.py` (Athena→local snapshot), `dashboard/data/opportunity_snapshot.parquet` (committed via a gitignore exception so it deploys self-contained), `dashboard` dep group (streamlit+plotly). Verified: ruff clean + data logic checked on real data. **UI runtime NOT smoke-tested** — streamlit/plotly wouldn't finish installing on the throttled connection; run `uv sync --group dashboard && uv run --group dashboard streamlit run dashboard/app.py` when the connection allows.

**Auto-logger (component 2 foundation) — committed.** The toxicity/ML collection loop the "is it just theory?" question needs: `ml/lp/ws_features.py` (captures live board microstructure) → `ingestion/ws_feature_storage.py` (→ S3) → dbt `stg_ws_features` → `fct_ws_markout` (each snapshot joined forward 30s → `flow_signed_markout_c`; >0 = toxic flow led price). Scheduled by `.github/workflows/ws-capture.yml` (game-window crons + dispatch). `docs/setup/09`. Verified offline (ruff/mypy/pytest 52/dbt parse). NOT live yet (no Glue table, no data).

**--ws maker bug FIXED (`998a718`).** The private `fill` channel was subscribed with market_tickers → went silent after the maker rolled markets (markets 2..N got no WS fills). Fixed: `subscribe()` now sends account-wide channels (fill) with NO market filter. Also made the fee REST-authoritative (a WS fill may omit fee_cost). pytest 53. Live verification still pending (auth session during a game).

**⚠ MERGE-TO-MAIN PREREQUISITE (or the nightly cron breaks):** `dbt build` in the scheduled pipeline runs ALL models. On merge to main it will try to build `stg_lp_*`/`fct_lp_*` and `stg_ws_features`/`fct_ws_markout`, whose Glue sources **do not exist** (`crypto_raw` has only coinbase_ohlcv, kalshi_btc_15min, kalshi_universe). BEFORE merging: create `lp_sessions`+`lp_fills` (docs/setup/07 DDL) and `ws_features` (docs/setup/09 DDL) under the **admin** profile, else the nightly build fails on missing sources. (kalshi_universe already created.)

---

## 2026-07-15 — CONSOLIDATION: "full Kalshi platform" — component (1) the universe-opportunity mart BUILT + verified offline; Kalshi Pro / fee / incentive research

Decided the project's coherent endgame is a **full-Kalshi screening+trading platform** (the 06-26 checkpoint already reframed the project as "the trading bot IS the DE project / finding edge on Kalshi"). Deep-research on **Kalshi Pro** (launched 2026-07-13, free public beta) + the 2026 profit landscape (3 workflow legs, session-limit-throttled; hand-synthesized from the journal + my own live API checks). Key verified facts:
- **Kalshi Pro** = free desktop workstation on the SAME public API (no exclusive endpoints): "Canvas" multi-market layouts, batch cancel / bulk edit / drag-to-reprice, and an **Active Markets Screener** over ~2,000 markets (price/spread/depth/5-min-vol) + trade tape. So a *real-time* screener is now first-party — our differentiated layer is the **historical/analytical** warehouse (markout/toxicity, fee/LIP-aware EV, calibration over time), not a live screener.
- **Fees (verified live via API `fee_type` field):** only **~131 of ~8,900 series** are `quadratic_with_maker_fees` (Fed/CPI, S&P/Nasdaq yearlies, NHL divisions, NCAAF, Emmys, BTC-max ladders) — the DMM-adjacent books. **All in-play soccer TOTAL/SPREAD stay `quadratic` = maker-free → the LP bot's economics are intact.** Maker fee (Feb-2026) = `0.0175·C·P·(1−P)` rounded up per fill → would erase a thin edge; the mart flags it as `has_maker_fee`.
- **Liquidity Incentive Program** (ends ~Sep 1 2026): open-retail, no application, DMM-firms excluded, two-sided-depth-required; but the incentivized-series list is **weather/altcoin/commodities/mentions — NO sports.** So the soccer bot earns nothing from LIP (my earlier "free money on top" was wrong). LIP-eligibility is NOT an API field (would need scraping kalshi.com/incentives).
- **Practitioner evidence agrees with our arc:** Kalshi 2021→Apr-2025 study — makers −9.6% vs takers −31.5% post-fee; the ONE positive cell = **makers on favorites (≥50c) +2.6% (σ 33%)** = literally our strategy. Winners are event-model specialists (CPI-formula, weather-methodology) + community sharps; a new entrant "loses their shirt." Institutional entry slower than hype (Jump/Susquehanna "dabbling"). MVE/combo parlays (live since Dec-2025, machine-readable `mve_selected_legs`) = the one genuinely fresh/immature book class.

**BUILT component (1) — the daily universe-opportunity mart** (S3→Glue→dbt medallion slice, mirrors kalshi_btc_15min/lp_storage conventions; designed via a 6-agent mapping workflow, implemented inline). Verified OFFLINE end-to-end (no AWS): ruff+mypy clean, pytest 49 (5 new), `dbt parse` clean, and a real local landing = **12,080 open markets / 1,795 series, Parquet schema byte-exact to the contract**.
- `ingestion/kalshi.py`: `get_series()` + `list_series()` (the latter returns all 11,480 series WITH fee_type in ONE call — replaced a per-series fan-out that took ~20 min over 1,795 series).
- `ingestion/kalshi_universe.py`: enumerate `/events?with_nested_markets` → filter two-sided + vol≥1 → attach fee_type from the one-shot map → `UniverseRow`; `ingest_snapshot()` + CLI (`--dry-run` field diagnostic, `--local-dir` offline path).
- `ingestion/kalshi_universe_storage.py`: `RAW_PREFIX=raw/kalshi_universe`, explicit `PARQUET_SCHEMA` (the Glue DDL contract), `write_universe_to_s3(..., local_dir=)` (S3 or local).
- dbt: `stg_kalshi_universe` (view: mid/spread/spread_c + `is_near_money`/`in_retail_band`/`has_maker_fee`) → `fct_kalshi_opportunity` (table: per-(series,snapshot_day), ranked by gross `spread_capture`, median + near-money spread, depth, maker-fee flag) + source/test sidecars (nested `arguments:` syntax).
- `docs/setup/08-kalshi-universe.md`: the CREATE EXTERNAL TABLE DDL (partition projection, range start 2026-07-15) + run sequence + admin caveat.
- `tests/test_kalshi_universe.py`: 5 tests (filter, one-call fee_type map, dt= partitioning, schema round-trip, local_dir, ingest summary) — FakeS3 + FakeClient, no AWS.

**NOT done (the AWS seam — needs creds, deliberately checkpointed):** create the Glue table under an admin identity (docs/08), `dbt build --select +fct_kalshi_opportunity` against Athena, and wire the daily CI step in `.github/workflows/pipeline.yml` + merge to main (the cron only fires on main; CI dbt-build will try to build the new models on next push, so land+create-table BEFORE pushing or that job fails on a missing source). All new work UNCOMMITTED. `spread_capture` is an explicit UPPER BOUND (not P&L, not toxicity-adjusted). Next of the 4-part plan: (2) toxicity/markout layer, (3) MVE combo-vs-legs scan, (4) Streamlit dashboard (= Project-1's missing serving layer).

---

## 2026-07-14 — NEW TRACK OPENED: Kalshi weather DIRECTIONAL (forecast-vs-market). Perps researched + explained + closed for small capital. Bonus: the Liquidity Incentive Program discovery.

Asked: any edge in Kalshi perpetuals or weather markets, and is there a DE project in either? Ran a deep-research pass (Kalshi docs/help/blog, CFTC filings; adversarial-verify pass still finishing — all claims below are primary-source) + live API microstructure probes. Verdict: **perps = understand + close; weather = the maker angle stays closed but the DIRECTIONAL question is open, measurable for $0, and is the right next platform build.**

**Perps, explained + closed.** Kalshi announced perpetual futures 2026-05-29, same day the CFTC issued a formal Reg-40.3 Order of Approval for BTCPERP (first US-regulated perp; NOT self-certification). Live as of July but production API access gated "member by member" (public demo). Contract: linear USD-margined, 0.0001 BTC (~$8 min), priced off **CF Benchmarks BRTI** (the same index the 15-min binaries settle on), 24/7, cleared by Kalshi Klear (their DCO). Funding = 8h TWAP of 1-min premiums, paid 12am/8am/4pm ET, clamped ±2%, <0.01% zeroed, peer-to-peer. Separate margin account (transferable; idle margin ~3.25% APY; portfolio margin via API). API = `/trade-api/v2/margin` (same auth/patterns, separate WS host) but NO batch orders / queue position / RFQs / historical endpoints. 12 altcoin perps filed, unapproved. **Edge verdict:** directional = no (BTC efficiency already proven here); funding arb = real but ~$0.10 per settlement per $1k (Kalshi's own example) → capital-gated, not for us; launch-basis dislocations are being handed to gated-in MMs; retail MM is out-tooled (no batch/queue + designated-MM tier). One parked note: **the perp book is a free 24/7 BRTI tick proxy** — the one alpha axis ever left open (sub-minute BRTI race) now has a data source; prior stays null, not reopening now.

**Weather — what the probes showed (2026-07-13, live API).** Daily high-temp ladders are HUGE: settled-day volume ≈ 199k contracts/day NYC, 465k/day LAX (one day 1.08M), ~135k CHI/MIA, ~45-67k AUS/DEN/PHIL → ~1M/day across 7 cities, WC-scale. Near-money buckets 2-4c spreads, two-sided; tails 1c. Hurricanes (KXHURCTOT) thin + 6-20c wide (seasonal, low priority). Mechanics: ~6 buckets/city/day, opens ~10am ET the day BEFORE, last trade 11:59pm ET day-of, settles next morning on the **NWS Climatological Report (Daily)** — final report, and the rules explicitly warn preliminary NWS data ≠ final (a trap AND a pipeline-accuracy edge). NOTE: the public API schema migrated to `_fp`/`_dollars` fields (volume_fp, yes_bid_dollars…) — old parsers read None.

**The two games.** (1) MAKING — measured dead 2026-06-2x (−0.44c/fill into end-of-day convergence pick-off; `ml/research/weather_logger.py`). Stays closed. (2) TAKING — untested: does a forecast/obs pipeline beat the price by more than spread + taker fee (0.07·P·(1−P) ≈ 1.75c at mid → need ~4-6c true near-money mispricing)? Two hypotheses: **H1 (model edge, pre-noon):** NBM/HRRR/ECMWF consensus vs market at fixed decision times. **H2 (nowcast edge, afternoon):** P(final high ∈ bucket | running ASOS max at t, climatological post-t warming) vs market — i.e., be the informed side of the same convergence that killed the maker. Honest prior: pro meteorologists trade these; H1 may be null (market ≈ NBM); H2 is an observation race but at 5-min cadence, not ms.

**THE PLAN (new package `ml/weather/`; phases gate each other; W0-W2 are $0 and read-only):**
- **W0 — calibration baseline + mechanics (~1-2 days).** Confirm settlement station IDs per city from market rules (NYC = Central Park/KNYC; LAX = KLAX; …) + the weather fee schedule. Harvest 60-90 days of candlesticks + settlements for **NYC + LAX only** (scope discipline; the volume kings). Respect the ~10k candlestick budget: fetch ±30min windows at ~8 fixed decision times, not full 39h ranges. Rerun the BTC-benchmark calibration harness (ECE/LL/favorite-longshot by city × decision-hour) → where is the market weakest? Script: `ml/weather/calib_study.py` (pattern: `ml/alpha/.../altcoin_efficiency`).
- **W1 — the NOAA/NWS data layer (the DE build, ~1 wk part-time).** Ingestion: IEM ASOS 5-min obs (historical archive + real-time, per station); NWS api.weather.gov point forecasts; **Open-Meteo previous-runs archive** (archived per-model forecasts — the fast path to H1 backtests without grib wrangling); CLI/CF6 settlement reports via IEM AFOS (final-vs-preliminary tracked). Stretch: NBM/HRRR grib2 from AWS Open Data (noaa-nbm-grib2-pds / noaa-hrrr-bdp-pds) → the impressive-artifact tier. Land raw → S3 → dbt `stg_weather_*` → `fct_weather_pit` (forecast-AS-OF + obs-AS-OF, PIT-correct like the crypto mart). Files: `ingestion/noaa.py`, `ingestion/weather_storage.py`.
- **W2 — the verdict (walk-forward, cost-aware, ~2-4 days once W1 lands).** H1: model-implied bucket probs (point forecast + historical error dist, or NBM percentiles) vs market at decision times; H2: running-max nowcast vs afternoon prices. Walk-forward, day-block bootstrap CIs, full cost model (cross the spread + taker fee). **Gates: net-of-cost ROI day-block 95% CI > 0 over ≥30-45 days AND split-half stable → W3. Straddles 0 → extend or park. Negative → kill + write up (a null here is still a complete platform story.)** Script: `ml/weather/edge_study.py`.
- **W3 — live pilot (ONLY if W2 passes).** Small taker bot, $25-50 exposure cap, staged ladder like lp_live, reusing the execution stack. Gate: live edge within CI of backtest over ≥10 days.

**BONUS DISCOVERY — check THIS WEEK: Kalshi Liquidity Incentive Program** (Sep 15 2025 → **Sep 1 2026**): $10-1,000/market/day reward pools, randomized per-second book snapshots scoring resting orders (size × proximity, 1.0x only at best bid/ask), pro-rata, ALL market categories eligible, designated MMs excluded. The soccer maker rests at the touch all day → we may be earning (or leaving unclaimed) program rewards. ACTION: check transaction history + the program's eligible-market list against our quoted series. Expires in ~7 weeks — "verify reward-incentivized series" has been in the backlog since 06-15; this is that answer.

**Context/parallel:** WC final is Jul 19 — keep running the soccer bot on the remaining game days (zero marginal effort); the weather build is read-only and doesn't compete. Post-WC soccer confirmation (club leagues) still gates LP scaling per the 06-26 checkpoint.

**RESULT — SAME DAY (2026-07-14): W0 shipped + H1 spike KILLED the model edge before building the pipeline.**
Built `ml/weather/calib_study.py` (W0) + `ml/weather/h1_spike.py` (the cheap go/no-go), ruff+mypy green, added to ml/README. Ran both live on 45d NYC+LAX.
- **W0 (calibration):** market well-calibrated (ECE ~2-4%); log-loss falls monotonically through the day (~0.30 morning → 0.13 @2pm → ~0.01 @6pm) as the high resolves; residual miscalibration concentrates in the MORNING (eve ECE 6.5% NYC / 4.8% LAX) — so H1 (morning model edge) was the only place to look, H2 (afternoon nowcast) faces an already-sharp book.
- **H1 spike (model vs market, ladder log-loss, lower=better):** the Open-Meteo day-ahead model is a FLAT baseline (NYC 1.231, LAX 1.014 — one static forecast); the MARKET improves through the morning and beats it: NYC market 1.170→1.081→1.051 (eve/06h/09h) all < model; LAX 1.033→0.998→0.944, model only "wins" LAX@eve by +0.019 (in-sample-best σ + a ~0-lead possibly-look-ahead forecast = noise; market argmax-hit 67% vs model 40% there anyway). Both thumbs were on the model's scale and it STILL lost.
- **VERDICT: H1 DEAD. The crowd already embeds the forecast and sharpens past a real NWP model as the morning progresses.** Same shape as the whole project — liquid Kalshi markets are efficient. H2 (afternoon nowcast) is an observation/latency race into an already-near-certain book (log-loss <0.13 by 2pm) = the BTC-tick-race shape that came back null; not worth chasing.
- **DECISION: PARK weather-directional.** The full NOAA medallion pipeline (W1) is NOT worth building for profit against a dead edge; it would only be a pure DE showcase. The honest arc — measured the edge for ~$0 in one session and killed it before a week of pipeline — IS the platform thesis working. Weather MAKING was already dead (convergence pick-off); weather TAKING now measured dead too. Weather closes as a track. Caveat (not pursuing): a fuller test (lead-pinned NBM forecast, more cities/days, ensemble spread instead of Gaussian-σ) could refine, but the prior is now firmly negative.

**▶ NEXT (weather closed):** (1) **incentive-program check vs the soccer bot** — the one live, expiring, positive-EV item (~Sep 1); (2) remaining WC game days on the soccer bot; (3) back to the 06-26 checkpoint's DE list (README rewrite, LP pipeline vs Athena, dashboard). Perps stays a someday side quest (demo funding-basis logger), not the thrust.

---

## 2026-06-17 → 06-26 — CHECKPOINT: the project became a live Kalshi market-making operation + its data platform (WebSocket multi-market infra built; edge confirmed soccer-structural, not yet net-proven)

Consolidated checkpoint over ~10 days (per-track detail is in the memory files; this is the map + resume point).

**Where the project is now.** The original DE platform (crypto ingestion → Athena/dbt PIT store → ML) did its honest job — it proved the predictive BTC-15m edge doesn't exist net of cost (~10 null axes). The project then pivoted to its real centre: a **live Kalshi market-making bot (real money) and the data platform around it**. The trading bot *is* the data-engineering project now ("finding edge on Kalshi").

**The trading edge — what we learned.**
- *Strategy:* two-sided liquidity provision on Kalshi in-play sports TOTAL/SPREAD markets — capture the bid-ask spread; needs mean-reversion + wide retail spreads + maker-free fills.
- *9-day record (current strategy):* net +$70, spread-capture +$61, **+0.59c/fill**, markout −0.24c, net-positive 7/9 days — BUT capture went **negative the last 2 days** (06-24 non-WC, 06-25 WC trending 1H-totals).
- **THE key finding — the edge is SOCCER-STRUCTURAL, not volume.** Per-sport: WC soccer markout −0.10c, positive **9/9** days; WNBA breakeven (4/8); MLB net-negative (3/10). Soccer scores rarely + discretely → totals/spreads mean-revert → benign to make; basketball/baseball score continuously → fair value trends → toxic pick-off. Corroborated by in-play-trading prior art (soccer = the #1 scalp market). So **`--prefix KXWC` (World-Cup-only) shipped**.
- *Size scaling:* 2× is **not** truly 2× — only ~1.4× real volume (fills are taker-flow-limited), per-fill edge flat, and it amplifies residual variance. **Bigger size = the worst axis.** The real scaling lever is **more markets (concurrency)** → which motivated the WebSocket build.
- *Confidence:* mechanism real ~70-75%; net-reliable-money **~50% and trending down** (the World Cup — the regime that drove the wins — is ending). So NOW = **confirm** (post-WC days + the `--ab` size test), not scale.
- *Closed, all null, with data:* tennis (martingale), Polymarket spread (1c) + reward farming (reward covers only 2% of the goal pick-off), Kalshi weather temp markets (maker *pays* 0.44c on a 1c spread into a guaranteed convergence pick-off), cross-venue arb, binary-perp basis. Through-line: every predictive/competed/toxic edge is null; the lone survivor is Kalshi retail-**soccer** MM.

**The data-engineering work (the new-idea direction — the bot as the DE project).**
- *LP data pipeline (built, parses + lineage green offline, NOT yet run against Athena):* `ingestion/lp_storage.py` lands the bot's session/fill CSVs to S3 raw Parquet → dbt `stg_lp_*` → `fct_lp_market_session` (per-session, enriched with ET-day/sport/type + the net = capture + residual − fees decomposition) → `fct_lp_daily` (the OOS tally as a model). DDL + run sequence in `docs/setup/07-lp-pipeline.md`. Mirrors the coinbase/kalshi medallion. Needs S3 creds + the Glue tables created to run end-to-end.
- *WebSocket multi-market infra (Phase 0-4, built + committed, read-only):* the scaling unlock. `ingestion/kalshi_ws.py` = async client — `LocalBook` from snapshot+deltas, **per-stream seq-gap detection**, auto-reconnect + `get_snapshot` gap-recovery, trade buffer. `ml/lp/ws_logger.py` = read-only multi-market logger — discover the active set → subscribe many on **one connection** → reconcile each local book vs REST → roll as games start/end → log `data/ws_book.csv` + `data/ws_trades.csv`. Verified live: **0 seq gaps** over thousands of deltas, local book REST-validated. This is the production-grade FEED the live multi-market maker (Phase 5) will reuse.
- *Codebase cleanup (committed to main):* the flat 34-script `ml/` was reorganized into `ml/alpha/` (closed BTC hunt), `ml/lp/` (active maker), `ml/research/` (closed side-tracks), + `ml/README.md`; all prior uncommitted work committed in logical commits.

**Direction.** Treat the **live Kalshi soccer-MM bot + its data platform** as the project. The edge is real but thin and WC-seasonal, so trading is in CONFIRM mode. The durable portfolio value is the **platform + the honest research arc** — built a platform to hunt edge, proved the easy edges don't exist, found one structural edge, built the infra to scale it — which holds regardless of whether the seasonal edge survives.

**WHAT'S LEFT (resume list).**
*DE / platform (the new-idea direction):*
1. **Run the LP pipeline end-to-end** — set `S3_BUCKET` + creds, create the Glue tables (docs/setup/07), `lp_storage` ingest → `dbt build` → `dbt test`. (Built, never run against Athena.)
2. **WS-data pipeline** — storage + dbt layer for `ws_book.csv`/`ws_trades.csv` (analog of lp_storage) so the multi-market feed feeds the warehouse.
3. **Per-sport / markout dashboard** — Streamlit over `fct_lp_daily` / `fct_lp_market_session`; turns the structural finding into a legible artifact.
4. **Toxicity / market-selection model** — predict markout from sport/type/activity/spread (the ML layer of the new DE project; the right target since price-direction is dead).
5. Wire the new LP CSV columns (`quote_size`, `count`) into `lp_storage` schema + dbt.
6. **The write-up** — refresh the top-level README to the real arc (still stale at "ML demo").
7. Push to GitHub (main is local-only, never pushed); delete the `admin` IAM key (hygiene).
*Trading / edge (gates the live scaling):*
8. Accumulate **~8-12 clean POST-WC days** → confirm the edge survives the World Cup ending.
9. Run the **`--ab` size test** (clean same-day 1×-vs-2×) → settle size scaling.
10. Verify the WS feed at SCALE during a game: `uv run python -m ml.lp.ws_logger --prefix KXWC --minutes 30` (read-only).
11. **Phase 5 (GATED on #8):** the live async multi-market MAKER — wire `kalshi_ws` into the quoting loop (② faster reaction + ③ more markets). Then **Phase 6:** deploy EC2/Fargate for unattended running.

**Git state.** On branch `ws-multimarket-logger` (3 WS commits ahead of main); `main` has the cleanup/reorg + LP pipeline + bot evolution + closed-track commits. Nothing pushed yet. The WS branch is ready to fast-forward into main when desired.

---

## 2026-06-16/17 — Liquidity-provision pilot: from screen → paper → LIVE real-money market-making (first ~4.6h, +$5.33 realized, edge unconfirmed)

A separate "can this actually make money" track (the platform's directional/arb alpha is closed — all null). Verdict from research: the easy alpha is competed away; structural money is in **liquidity provision** (get paid the spread + maker rebates). Polymarket US is iOS-only with no public trading API → **Kalshi first** (full API, maker fills free).

**Built the full stack this session:**
- `ml/lp_market_screen.py` (Stage 1, opportunity landscape via `/events`): Sports = ~70% of the gross spread-capture prize but at 1–2c spreads (crowded/efficient); the sweet spot is moderate-spread + real flow.
- `ml/lp_toxicity_screen.py` (Stage 2, flow markouts): public minute candles can't resolve the sub-minute adverse selection that decides maker P&L (BTC control read benign — its toxicity is sub-minute) → the decisive test needs a **live pilot**.
- `ml/lp_pilot.py` (paper v2): inventory-capped, kill switch, smooth-market selection. Validated, but fill rate is an upper bound (queue ignored).
- `ml/lp_live.py` (Phase B, REAL orders): signed POST/DELETE added to the Kalshi client. Staged ladder `--auth-check → --test-order → --live --i-understand-live` (each ~zero-risk; a wrong binding fails as a rejected order). V2 order binding confirmed (`/portfolio/events/orders`, side bid/ask, $-string price, post_only). Inventory **skew** (mean-reverts to flat), **fractional** fill tracking (markets allow partial fills like 0.36), auto-flatten (aggressive IOC), dead-book exit (market resolved), **market-rolling** (roll to a fresh market as each resolves so `--minutes` is wall-clock), per-market + session kill switch, session + per-fill **markout logging** (`data/lp_sessions.csv`, `data/lp_fills.csv`).

**Result (`ml/lp_analyze.py`): 14 markets, 4.6h, 898 fills. REALIZED +$5.36 gross / +$5.33 net of fees ($0.03), 12/14 markets positive; account ~doubled ($5→$11, incl. ~$1.83 of favorable residual settlements on resolved markets = directional luck, not edge).** The edge signal — fill-weighted **markout = −0.04c/fill, session-block 95% CI [−0.18, +0.08]c** → straddles zero: **no measurable adverse selection (the right MM signature) but no confirmed positive edge either.** So the P&L is **spread capture under ~neutral flow** (~+1c/round-trip), not prediction.

**Why some sessions paid and others didn't:** productivity = volume (fills = flow × duration) × per-fill edge. The winners were **high-volume full-game markets** (WNBA total +$2.29/265 fills, NCAA game +$1.04/194, spreads +$1.04) with sustained two-sided retail flow; the duds were **illiquid niche props** (WC "mention": 78 min, 51 fills, ~$0) and **sharp in-play swings** (a tennis match markout −1.1c = a point ran the maker over). So: favor liquid, popular, full-game total/moneyline markets; avoid niche + sharp-event props.

**Honest verdict:** promising but NOT confirmed. Tiny sample (one afternoon, specific games), P&L concentrated (~62% from 2 markets), markout CI includes zero, and ~$5 absolute is meaningless on $5–11 capital. Need many more sessions across days/regimes to tighten the markout CI. **Scaling (only once confirmed):** (1) verify which series are reward-"incentivized" (rebates on top of spread); (2) concurrent multi-market quoting (uncorrelated markets → P&L variance ~1/√N → faster confirmation + diversification) — needs an async loop + shared risk limits; (3) larger size — but bigger orders worsen own queue position/fill rate, so size carefully. Full record: memory `project_lp_market_making_track.md`.

---

## 2026-06-11 — the W+9–13 cluster VERDICT from the Lambda books: not an execution artifact — *no edge at all* out-of-period (cluster CLOSED)

The Lambda collector ran flawlessly while Derek was away (06-05→06-12 UTC: 288 files/day = 96 windows × 3 bursts at true offsets ~W+1:50 / ~W+12:51 / ~W+14:50; `btc_spot` populated from 06-05 06:14 — the UA fix was redeployed). That gave **~650 settled windows over 8 fresh days with a LIVE executable book + simultaneous spot at the cluster minute** — vs the ~40 W+1-only windows everything previous rested on. Session run autonomously per Derek's ask.

**Did — `ml/live_cluster_verdict.py` (ruff+mypy clean, re-runnable as data accrues).** Loads the synced S3 snapshots, joins Kalshi settlements + decision-minute candles (batch endpoint, chunked under its ~10k-candle budget — full-day ranges 400) + Coinbase minute bars, fits the displacement logistic on the 6,100-window warehouse history STRICTLY before 06-05 (forward/OOS by construction; warehouse itself is frozen at 06-03 since local Airflow didn't run), and prices the SAME strategy three ways per window: (1) decide+fill at the W+k candle close (the sweep's assumption), (2) the same bets filled at the live touch, (3) full live replay (decide from the snapshot's own spot against the live ask). Day-block bootstrap CIs throughout; parameterized `k ∈ {2, 13}`.

**Result — the cluster fails at its OWN prices, and execution is a wash at W+13:**
- **W+13** (650 windows): backfill-candle leg **−3.7% [−6.5%, −1.4%]**; same-bets-live-fills −3.7%; live replay −2.7% [−6.0%, −0.3%]; naive follow-move −2.8% both pricings. The in-period sweep cluster (~+5–7%, W+12 peak +7.2% [+4.2,+10.1]) doesn't even CI-overlap the fresh period.
- **Execution is NOT the killer at W+13:** paired (leg1−leg2) cost +0.1% [−0.3%, +0.4%]; side-conditional slippage mean +0.04c, median 0, only 34% adverse. The live book ≈ the candle at the decision instant. The signal itself has no out-of-period edge.
- **W+2** (653 windows): backfill leg **−5.4% [−11.9%, +2.0%]**, live replay −5.0% — the W+1-style displacement edge is gone this period too. Here the lead-lag mechanism IS still visible where it should be: naive follow-move −2.4% at candle → −5.6% at live fills, and the burst mid still chases spot (R²=0.75, +17c/$100, n=658 — replicating the 37-window R²=0.92/+24c finding, attenuated at scale). At W+13 the coupling is much weaker (R²=0.33): near expiry the book is gappier/more discrete, not faster.
- **W+15 books are DEAD: only 17% of last-minute snapshots have a two-sided quote.** Even a real near-expiry signal would have nothing to trade against (and no exit) — the operational closure of the settlement-lag thread at minute resolution.

**Verdict: the W+9–13 "profit cluster" was in-period selection noise (max over 14 minutes in one regime), not a tradeable edge and not even a stable execution artifact.** Exactly what the sweep's own warnings (±3–4% day-block CIs, split-half +0.21) predicted — the uncertainty discipline called it before the live data confirmed it. Displacement-family strategies are now negative at every tested decision minute, at both backfill and live prices, out-of-period.

**Learned:**
- An in-period day-block CI is still conditional on the period AND the selection (picking W+12 of 14 minutes biases it up); only a fresh-period replication is an honest test. The cluster's [+4.2,+10.1] vs the fresh [−6.5,−1.4] is the cleanest demonstration of that in the whole project.
- "Artifact vs real" was the wrong dichotomy — the third option, "neither survives the period," is what actually happened. Test the signal out-of-period BEFORE explaining its mechanism.
- Kalshi's batch candlesticks endpoint budgets `n_tickers × range_minutes` (~10k); chunk windows, not days.
- The platform asymmetry showed its value: serverless collection kept running unattended; the laptop-bound Airflow batch (warehouse frozen at 06-03) did not. The irreproducible data (live books) was the part that survived — by design.

**▶ NEXT:**
1. **The write-up (README + dashboard) is now THE move** — the README status table still says "ML: Planned" (stale since 06-01) while the actual arc (benchmark → leak-hunt → +8% saga → live verdicts → 10 nulls) is the portfolio centerpiece. No remaining analysis blocks it.
2. The Lambda keeps accruing (~$0/mo); `ml/live_cluster_verdict.py 13|2` can be re-run as days pile up to tighten the CIs, but with every leg negative the prior is firmly closed. Consider stopping the EventBridge rule once the write-up freezes the numbers (or keep it for the dashboard's live tile).
3. `ml/live_paper_pnl.py` (full-feature forward test) stays blocked on the batch pipeline being run — moot for alpha (cluster closed), but running one `dbt build` + re-run would tie off the W+1 thread with the production model for completeness.
4. Push to GitHub: main is 4 commits ahead (the entire Lambda collector + 3 analysis sessions exist only locally).

---

## 2026-06-05 (cont.) — Deribit options-implied + market-making: both null (the last two threads close)

Ran the final two backlog angles — the only ones bringing *new* information (options) or a *different role* (providing vs taking liquidity).

**Did — Deribit options-implied direction (`ml/options_implied.py`).** Live BTC option chain → shortest-expiry ATM IV 87.7% (1σ 15-min move 0.47% ≈ $300), risk-reversal −19 vol pts (heavy put skew). Risk-neutral **P(BTC up over 15 min) = 0.4991** → a **−0.09c** directional edge vs the ~1c spread. **Null by construction, not just empirically:** over 15 min the risk-neutral drift is negligible (N(d2)→0.5) and even a big skew moves it a sub-cent fraction. Options price *volatility*, not 15-min *direction* — so "import the smart-money options view" adds no usable directional signal. (It would matter for a vol/range/straddle market, which KXBTC15M isn't.)

**Did — market-making feasibility (`ml/market_making.py`).** From 138 decision-minute order books: median spread 1.0c (half-spread 0.5c earned), mid-drift 0.17c/s (adverse selection from the lead-lag), Kalshi fee ~2.0c at mid. **Breakeven repricing latency τ* = 3s** (no fees) — a passive MM must re-quote within ~3s or the drift eats the half-spread → the SAME latency race as the taker side. And the **2c fee alone exceeds the 0.5c half-spread**, so unless Kalshi rebates makers, MM loses on fees *before* adverse selection enters. Providing liquidity is no free lunch — same wall, other side of the book.

**This closes the alpha hunt completely.** Every reasonable angle is now tested and null: model class, derivatives (funding), order flow, the +8% lead-lag (latency artifact), favorite-longshot, settlement-lag (minute), threshold-ladder RV, less-liquid ETH/HYPE (friction-inefficiency), options-implied, and market-making. The only untested threads (W+12 live cluster, ETH forecasting) need unblocking and carry a strong null prior. The market is efficient to the limits of arbitrage; the deliverable is the platform + the rigor.

**Learned:** options inform vol not short-horizon direction (a structural, not empirical, null — worth knowing for instrument selection); market-making is the *mirror* of the taker race plus a fee hurdle, so "be the MM" doesn't escape the wall.

**▶ NEXT:** the write-up (README + dashboard of the whole honest arc) is now unambiguously the move — the hunt has reached a complete, conclusive end.

---

## 2026-06-05 — ETH/HYPE efficiency test: thinner markets ARE less efficient, but untradeable (the friction–inefficiency tradeoff)

Tested the "less-liquid → exploitable" hypothesis directly on the #2/#3 Kalshi 15-min markets (ETH, HYPE — confirmed by 24h volume: BTC 33k, ETH 1.3k, HYPE 0.3k). `ml/altcoin_efficiency.py` fetches each settled window's decision-minute implied prob + outcome straight from the public API (reusing `kalshi_backfill._fetch_candles`; no warehouse, no OHLCV needed) and runs calibration + favorite-longshot, with BTC fetched alongside as the calibrated reference.

**Result (45d, ~4,200 windows each):**
- **BTC** (1c spread): log loss 0.658, ECE 0.018 — the informative, calibrated bar; favorite-longshot null (−2..−4.6%). Reproduces the warehouse numbers → method validated.
- **ETH** (2c spread): log loss 0.658, ECE 0.015 — **just as efficient as BTC**; favorite null (−0.1..−4.1%). The 2× spread only makes it costlier. Hypothesis FAILS for ETH.
- **HYPE** (9c spread): log loss **0.693 (= coin-flip, NO information)**, ECE 0.069 (4× worse), deep-favourite bin [0.9,1.0) realized 0.70 vs priced 0.92 — **measurably LESS efficient.** Hypothesis CONFIRMED directionally. BUT betting the favourite loses **−13..−14.5%** at every depth — the 9c spread obliterates the mispricing.
- The deepest-favourite (≥0.90) +ROI in all three is n<15 noise (100% win on 8–12 bets) — same small-sample artifact as the BTC favorite-longshot bin.

**The insight (arguably the best of the whole alpha hunt):** the efficiency↔liquidity relationship is REAL — BTC (most liquid) is most efficient, HYPE (107× thinner) is genuinely inefficient. But **friction scales WITH the inefficiency**: the uncontested market is mispriced *because* no one arbs it, and that same lack of competition is *why* the spread is 9c. So the inefficiency is always trapped inside the friction → untradeable. Markets are efficient "to the limits of arbitrage," shown directly across three assets. No money — but a clean, satisfying answer to "what about less-liquid markets?".

**Learned:**
- Caught my own bug live: favorite-DEPTH cutoffs must be on max(p,1−p) (≥0.5..0.9), not the bet-rule margin (0/0.05/0.10) — else they never filter (fav_prob is always ≥0.5).
- "Less liquid → exploitable" is half-right: less liquid → less efficient (true for HYPE) but proportionally wider spread (so you still can't capture it).
- Market-internal tests (calibration / favorite-longshot) need ONLY Kalshi price + outcome → portable to any asset with zero ingestion. That's why this was one script, not a pipeline.

**▶ NEXT:** an ETH forecasting/lead-lag test would need ETH-USD OHLCV features — check whether `fct_features_pit` already carries ETH rows (the Coinbase ingest may have done BTC+ETH); if so it's a cheap in-memory test, else defer (prior = another null, ETH priced as well as BTC). Remaining: market-making (hard); the W+12 Lambda cluster verdict (pending redeploy + data accrual).

---

## 2026-06-04 — Lambda collector LIVE; "can we make money even with latency solved?"; threshold-market RV scan (null)

Continuation of the +8% saga: graduated collection to AWS, reasoned out the real capturability wall, and started testing the remaining *no-race* edges.

**Did — Lambda order-book collector deployed + fixed (`lambda/orderbook_collector/`, CloudFormation):**
- Deployed via `AWS_PROFILE=admin scripts/deploy_orderbook_lambda.sh` (deploy needs admin; the local `crypto-de-pipeline` user is S3-only). Verified: manual invoke → 200 + wrote a snapshot to `s3://…/raw/orderbook_snapshots/`. EventBridge fires it 24/7 at W+1/W+12/W+14 of every window. Cost ~free (Lambda always-free tier; EventBridge scheduled rules free; S3 cents).
- **Bug found+fixed (commit c7072df, redeploy pending):** every deployed snapshot had `btc_spot=None` — Coinbase (Cloudflare) 403s the default Python-urllib User-Agent. Added a browser UA; verified spot populates locally. Handler stays dependency-free (stdlib + boto3) so the zip is just handler.py.
- Retired the local launchd collector + caffeinate (no more Mac dependency / sleep gaps). Capture offsets land at wk~2/10/13/15 (EventBridge jitter + the 40s burst), not exactly 1/12/14 — but each snapshot records its true wk and wk10/13 bracket W+12, so usable.
- Cost check: month-to-date AWS = **$1.34**, ~all of it 266k S3 PUT/LIST requests from heavy *Athena query iteration* this session — NOT the Lambda, NOT storage. One-time dev cost; mitigable with Athena result-reuse + caching `load_training_frame` locally (it hits Athena every run).

**Did — capturability reasoning (the honest "even if latency weren't an issue?"):**
- We measured the decision→order loop only as a market-data GET *proxy* (~73ms warm, ~50–200× inside the ~30s breakeven) — **never an actual fill.** The real unknown isn't latency-in-ms, it's **fill QUALITY**: at execution do you get the *stale* (pre-reprice) price or the *repriced* one? Only answerable by actually placing orders (Kalshi demo → tiny real; the RSA-PSS signer exists, unused).
- Structural verdict: even with latency "solved" for us, the lead-lag is an adversarial **race against co-located MMs** — the book reprices in seconds *because they're already racing*. We'd be the picked-off slow player. So **no realistic money in 15-min BTC direction; the wall is competition, not our wiring.** Logged the 3 remaining *no-race / untested* angles: threshold-market RV, less-liquid ETH/SOL 15-min, market-making.

**Did — threshold-market static-arb scan (`ml/threshold_arb.py`) — NULL (clean no-race test).** KXBTCD/KXETHD ("X ≥ strike at expiry?") have strike ladders where P(≥K) must be non-increasing in K. Snapshotted the live ladders and checked (1) mid-monotonicity and (2) the EXECUTABLE arb — buy YES@K1 + NO@K2 (K1<K2) pays ≥$1 in *every* outcome, so cost <$1 net of fees = free money. Result: small mid-violations (worst +9.5c BTC 59.2k→59.3k, +3c ETH) but **0 executable arbs net of fees on any ladder, including the thinner ETH** → the mid inversions sit inside the spread+fee. Ladders internally consistent within costs = efficient. Caveat: single snapshot; transient arbs during fast moves / repeated sampling untested.

**Learned:**
- "Latency solved" (absolute ms) ≠ "winning the race" (being faster than the MMs who *set* the reprice speed). The binding constraint on the lead-lag is competition, not infra.
- **Fill quality > fill latency:** even instant fills don't profit if the favorable resting quote is already gone — only live order placement measures it.
- For a 2-leg static arb the **mid is misleading**; only executable touch prices (asks) net of fees decide, and mid "violations" routinely sit inside the spread.

**▶ NEXT:**
1. Redeploy the Lambda (push the spot fix), let it accrue ~2–3 days (~100 windows/day) → run the W+12 reconciliation to settle the cluster.
2. Remaining no-race frontier: ingest ETH/SOL 15-min (KXETH15M/KXSOL15M) for the less-liquid direction/lead-lag test; (optional) re-run the threshold scan during a fast move; market-making is hard (adverse selection).
3. (If ever settling capturability for real) Kalshi demo account → place paper/tiny-real orders → compare fill price to decision price.

---

## 2026-06-03 (cont.) — the +8% VERDICT: live-execution reconciliation + favorite-longshot null (alpha hunt CLOSED)

Picked up the overnight order-book collector and settled the last open question in the whole project: **is the +8% backtest tradeable, or a lead-lag artifact?**

**Did — live-execution reconciliation (`ml/live_exec_reconcile.py`, ruff+mypy clean):**
- Collector banked **40 decision-minute windows** overnight (`data/orderbook_snapshots.jsonl`, 3 snaps/20s each); 37 reconciled. Confirmed the launchd alignment: the backtest prices at the first in-window candle's CLOSE (`event_at = end_period_ts = W+1:00`), and the :01 fire snaps at ~W+1:00 — so live-vs-backfill is a fair, same-instant comparison.
- **Method (model-free, chosen over a full model re-score):** the live data only changes ONE backtest input — the execution price (model prob + settlement are unchanged) — so I isolated it. Per window: live executable ask (snap nearest W+1:00) vs the backfill candle ask the backtest used; reused `ml.backtest._summarise / _effective_quote / _breakeven_cost` to map the measured slippage onto the real 6,132-window backtest.
- **Finding 1 — NOT a stale-quote artifact.** Decision-instant slippage is UNBIASED: pooled ask mean +0.03c, median 0c (std ~4c). The price the backtest assumed was real and hittable. (This is what the earlier 5-way leak-hunt couldn't prove; now proven against live data.)
- **Finding 2 — the edge is friction-bound and latency-sensitive.** The book reprices ~3.8c per 20s. Spot-tracking regression (the tightener, to refute "zero-mean drift is free"): within-burst **mid move tracks BTC spot at R²=0.92, r=+0.96, +24c/$100, 94% sign agreement** → the drift is structural and ADVERSE to a momentum bettor (you and the market react to the same move; betting with it, your side's cost rises before you can fill).
- **Mapping to the +8%:** at recorded price +8.03% (reproduces the backtest exactly — sanity check); charging the zero-mean instant scatter (~2.9c, a *pessimistic* bound) → +3.9%; charging one 20s latency drift (~3.8c, *legitimate* since adverse) → +2.9%; breakeven ~6.2c is only ~2 such delays away.
- **VERDICT: a real but THIN, latency-bound microstructure edge — exactly the size of the execution friction protecting it — uncapturable by a 15-min batch pipeline. Textbook efficient market.** Committed `655ccda`.

**Did — favorite-longshot / tail-mispricing test (`ml/favorite_longshot.py`, clean):** a structural edge that needs NO forecast and NO fast execution — does the market overprice longshots / underprice favorites (the near-universal betting-market bias)? Reused `ml.metrics.reliability_table` + `_summarise`. RESULT: **NULL.** Calibration clean across all bins (every |z| < 1; the bias is absent even in the tails); betting the favorite loses −3.4..−3.9% at every depth cutoff; betting the longshot loses −5..−8%. The lone +31% deep-longshot bin is 18 bets / +$0.48 of noise pointing the *wrong* way. Market prices the tails fairly too.

**Did — CORRECTION (same session, after pushback): walked back "uncapturable / alpha hunt closed" (`scripts/measure_execution_latency.py`).** I'd jumped from "latency-bound" to "uncapturable" without measuring latency. Fixed:
- **Edge-vs-latency curve** (re-priced the backtest universe at the measured ~0.19c/s drift): ROI +7.8% @1s, +6.5% @5s, +5.1% @10s, +2.9% @20s, **breakeven ~30s**. So a ~20s half-life — not instant death.
- **Measured the real decision→order loop** (residential Mac): Kalshi GET round-trip ~297ms, Coinbase ~95ms, inference 0.16ms → **end-to-end ~0.6s = ~50x headroom** under the ~30s breakeven. **Latency is NOT the binding constraint.** Capturability now hinges only on what offline analysis can't settle — fill probability at the touch, slippage at size, adverse selection, out-of-sample decay → a LIVE EXECUTION TEST.
- **Alpha hunt is NOT "fully closed":** probed Kalshi and found **KXETH15M + KXSOL15M** 15-min markets EXIST (uningested; less liquid → likelier mispriced), plus daily threshold markets KXBTCD/KXETHD (many strikes → options-implied / term-structure / cross-strike). And W+1 was a CHOICE — near-expiry (W+13/14) probes the ~10s BRTI settlement lag, a structural NON-latency-bound edge. Gate caveats keep ~0.6s conservative: GET-as-order-proxy (real POST adds RSA-PSS auth + match step), residential (cloud faster), cold connections (keep-alive faster).

**Did — forward paper-trading harness (`ml/live_paper_pnl.py`, ruff+mypy clean):** the out-of-sample live-execution test, built leakage-safe by REUSING the platform instead of re-porting dbt features in pandas. The collector captures the live touch in real-time; this fits the production logistic on every window STRICTLY BEFORE the captured block (a forward / out-of-sample fit) and tallies paper PnL by the backtest's exact bet rule + cost model, filling at the **LIVE touch** (with the backfill candle shown alongside as the backtest's assumption). Forward by construction — it can only score captured windows that have since SETTLED into the warehouse, so the readout grows daily as the collector + batch pipeline accumulate. First run = **WIRING PROOF only**: the batch pipeline is ~11h behind, so just 9 of 40 captured windows are in the marts → PnL is noise at n=9 (live touch +3% on 9 bets vs backfill −20% on 7; the bet COUNTS differ because the model clears the ask on 2 extra windows at the live price). Verdict accrues over days; re-run as windows pile up.

**Did — settlement-lag / decision-minute sweep (`ml/settlement_lag.py`, ruff+mypy clean) — NULL at minute resolution.** Tested "why decide at W+1?" + the BRTI-lag thesis by sweeping the decision minute k∈{1,5,10,13,14} (existing warehouse only: per-minute `crypto_staging.stg_kalshi_btc_15min` price + `crypto_marts.fct_features_pit` BTC close + `fct_kalshi_15min_label`). (1) The market CONVERGES hard toward expiry — log loss 0.659→0.095, accuracy 60%→96%, confidence 0.20→0.92 — pricing the near-determined outcome correctly. (2) An out-of-sample spot-displacement logistic is a WORSE forecaster than the market at EVERY k (disp_LL > mkt_LL throughout) → the market is NOT underreacting to observable spot → the minute-level settlement-lag edge is NULL. (3) The positive disp_ROI at k≤13 (+5–7%) is NOT structural: a worse-log-loss model can't have real skill, so it's the SAME within-minute lead-lag artifact as W+1 (betting vs the lagging candle close; already shown latency-bound in `ml/live_exec_reconcile.py`), and it dies by W+14 (prices at 0.99/0.01, no room). The genuine last-~10s BRTI race is sub-minute → needs TICK CAPTURE of the final seconds (a future collector extension), not minute candles. Confirms (with the favorite-longshot null) that the warehouse-resolution edges are all efficient-market nulls; the only live unknowns left are the forward paper test + the structurally-different markets.

**Did — per-minute decision sweep WITH uncertainty (`ml/decision_minute_profit.py`, ruff+mypy clean)** — answering "which minute is most profitable, and do we have enough data?". Swept k=1..14 with a day-block bootstrap 95% CI per minute + a split-half stability test. The displacement-strategy ROI rises from W+1 to a W+9..W+13 cluster (~+5–7%, nominal best W+12 +7.2% CI [+4.2,+10.1]) then collapses at W+14 (prices 0.99/0.01, no room). But the data-sufficiency verdict is the point: the effective sample is ~41 OUT-OF-SAMPLE DAYS (walk-forward scores only the later ~60% of the 70-day timeline), so day-block CIs are ±3–4% and the mid-window minutes overlap heavily → you CANNOT crown a single best minute (split-half ROI correlation only +0.21). And the whole ROI is the latency-bound lead-lag artifact (collapses at real execution) over one regime; we have order-book snapshots only at W+1 so can't even confirm W+12 reprices as fast. Verdict: not enough data (nor the right kind) to claim a tradeable best-minute edge.

**Did — graduated the order-book collector to AWS Lambda (`lambda/orderbook_collector/{handler.py,template.yaml}` + `scripts/deploy_orderbook_lambda.sh` + `docs/setup/06`).** To test whether the W+9–13 cluster is real we need decision-minute books at W+12 (we only have W+1), collected reliably 24/7 (the Mac launchd job has sleep gaps). Built a DEPENDENCY-FREE Lambda handler (stdlib + boto3-from-runtime; no httpx/cryptography/pyarrow to bundle → the zip is just handler.py) that snapshots the live book + BTC spot 3×~20s and writes JSONL to S3 (`raw/orderbook_snapshots/dt=…/<ticker>_wk<k>.jsonl`); a CloudFormation stack wires a least-privilege IAM role + the function + an EventBridge cron firing at W+1/W+12/W+14 of every window (minutes 1,12,14,…). Smoke-tested the handler LOCALLY → wrote a real snapshot to S3 with the pipeline creds (validated the whole capture→S3 path). NOT yet deployed: creating the role/function/rule is beyond the S3-only `crypto-de-pipeline` user, so the deploy is handed off (`AWS_PROFILE=admin scripts/deploy_orderbook_lambda.sh`). Honest framing set with Derek: more public data sources + fancier models will NOT enlarge the (efficiently-priced) direction edge — both already null — and Lambda CONFIRMS/REFUTES the cluster (prior = the latency-bound artifact), it doesn't manufacture alpha. The real frontier is the sub-minute BRTI race (this collector enables it), ETH/SOL, and the threshold markets.

**Learned / framing:**
- **Report uncertainty on any strategy ROI; the honest unit is ~tens of DAYS, not thousands of windows.** Testing 14 minutes and picking the max is noise-mining unless paired with bootstrap CIs (resampled by DAY, the real independent unit) + a split-half stability check. A point ROI without those is meaningless here.
- **Don't conclude "can't" without measuring it.** I asserted a batch pipeline executes too slowly; the actual loop is ~0.6s with 50x headroom. The reviewer (Derek) was right to push — "uncapturable" was an assumption dressed as a finding.
- The model-free reconciliation is cleaner than a full live re-score: when an experiment changes exactly one variable, isolate it instead of rebuilding everything (the live windows aren't in the marts anyway, so re-scoring would mean re-ingesting + risking dbt-feature leakage in pandas).
- Distinguish a *pessimistic* cost charge from a *legitimate* one: zero-mean fill scatter doesn't bias expected PnL, but spot-tracking drift does — so the spot-tracking R² is what licenses the latency haircut. Charging "abs drift" without that proof would be hand-waving.
- The honest close is a STRONGER portfolio story than a fake +8%: "found an apparent edge → stress-tested 5 ways → built a live experiment → showed it's a latency-bound artifact, the size of its own friction." Demonstrates I can tell microstructure from alpha and will kill my own result.
- **Alpha hunt fully exhausted across every axis:** model class, slow derivatives, fast microstructure, execution timing, AND price-shape (favorite-longshot) — all null. 15-min BTC direction is efficiently priced w.r.t. reasonable public info. The deliverable is the PLATFORM + the rigor of the no-edge proof.

**▶ NEXT:**
1. **Stop the pilot infra** (no longer collecting for a purpose): `launchctl bootout gui/501 ~/Library/LaunchAgents/com.derekkuang.{kxbtc-orderbook,stay-awake}.plist`. Keep the JSONL + the reconciliation as the artifact.
2. **Write-up: dashboard + README** telling the honest arc — benchmark (market is the bar) → leak-caught baseline → +8% saga → 5-way leak-hunt → live-execution verdict → favorite-longshot null. This is the portfolio centerpiece.
3. Optional remaining backlog angles (low priority, all likely null): Deribit options-implied prob, BRTI settlement lag (~10s structural, near-expiry), cross-market/term-structure — see memory `project-alpha-strategy-backlog`.

---

## 2026-06-03 — Phase 1 ML: model, the +8% backtest saga, alpha hunt (all axes null), live execution test

**Did — ML layer end-to-end (`ml/`, all strict-mypy + ruff clean):**
- `ml/data.py` (Athena→pandas loader), `ml/metrics.py` (log loss / Brier / ECE + reliability), `ml/walkforward.py` (expanding-window splits — never shuffle), `ml/model.py` (reusable walk-forward OOF + logistic & LightGBM).
- Benchmark first: Kalshi `implied_prob` near-perfectly calibrated (ECE 0.5%), **log loss 0.659 = the bar**.
- Honest baseline: caught + fixed a feature leak (`kalshi_mid_price` ≈ benchmark leaking into "BTC-only" features → `MARKET_COLS` exclusion). Clean walk-forward logistic ≈ **TIES** the market (LL 0.655 vs 0.662).

**Did — the +8% backtest saga (`ml/backtest.py`):**
- Cost-aware walk-forward PnL (real spread mid±spread/2 + Kalshi fee). Surprise: **NET PROFITABLE +8% ROI**, robust to 3× spread / +2c slippage (breakeven ~5.8c).
- No-skill CONTROLS (anti-model −15.6%, random/fade/follow all lose) → signal, not a PnL bug. 5-way leak-hunt cleared cost-bug / head-start / decision-alignment / bad-price.
- LIVE order book (`ingestion/kalshi.py::get_market_orderbook`) REFUTED "illiquid quote": real 1c spread, ~30k resting/side, executable.
- **Unresolved gap:** backtest priced at the candlestick CLOSE, but the live decision-minute price moves several cents in *seconds* (lead-lag) → the +8% may be untradeable. Built a live collector to settle it.

**Did — alpha hunt, all three axes NULL:**
- Model class (LightGBM): no help, overfit (LL 0.659, ECE 0.031).
- Slow derivatives (Deribit funding, `ingestion/deribit.py` + `ml/derivatives.py`; US-accessible — Binance/Bybit geo-block 451/403): no signal (0.655→0.656).
- Fast microstructure (Binance Vision aggTrades, `ingestion/binance_flow.py` + `ml/orderflow.py`; **53.3M trades → 96,480 minute rows** of taker OFI): no signal (0.655→0.656). **DEFINITIVE: 15-min direction efficiently priced w.r.t. reasonable public info.**

**Did — live execution test infra (LEFT RUNNING):**
- `ingestion/kalshi_orderbook.py` + `scripts/collect_orderbook.sh` → launchd `com.derekkuang.kxbtc-orderbook` fires :01/:16/:31/:46, snapshots decision-minute book + BTC spot → `data/orderbook_snapshots.jsonl`. Mac kept awake via launchd `com.derekkuang.stay-awake` (`caffeinate -i -s`).

**Did — strategy research (web):** logged 5 untested alpha angles to memory `project-alpha-strategy-backlog` (favorite-longshot, Deribit options-implied, BRTI settlement lag ~10s, cross-market/term-structure, market-making).

**Learned:**
- Tiny log-loss edge ↔ large betting edge: with price noise ε, LL gap ~ε² but betting edge ~|ε| (0.007 LL → ~5c edge is consistent, not a bug).
- Controls catch cost/accounting bugs but NOT look-ahead; only a live-execution test settles "is the backtest price tradeable?".
- Model class isn't the lever when features are already priced — alpha needs NEW info or to STOP forecasting (import/structural/settlement edges).
- Kalshi BTC settles on CF Benchmarks BRTI (laggy TWAP ~10s) — structural near-expiry angle. Whole serverless platform ≈ $1/mo; Lambda collector ≈ free vs ~$4-8/mo for idle EC2.

**▶ NEXT (fresh session — pick up here):**
1. **Check overnight data:** `wc -l data/orderbook_snapshots.jsonl` (collector + stay-awake left running).
2. **THE +8% VERDICT — live-execution reconciliation:** per captured window, join live touch price + reconstructed model signal + settlement outcome → PnL at REAL live prices vs the +8% backtest (expect it to collapse, confirming the lead-lag artifact). Reuses `ml/backtest.py::_summarise`.
3. **Cheapest new alpha shot:** favorite-longshot / tail-mispricing + within-Kalshi term-structure consistency on existing data (see memory `project-alpha-strategy-backlog`).
4. Then: Deribit options-implied prob; optionally graduate collector to AWS Lambda; **commit the session's `ml/` + ingestion work (currently uncommitted)**.
5. Cleanup when done: `launchctl bootout gui/501 ~/Library/LaunchAgents/com.derekkuang.{kxbtc-orderbook,stay-awake}.plist`.

---

## 2026-06-02 — dbt Kalshi layer: implied-prob feature (PIT-safe) + forward label

Built the transformation layer that turns the raw Kalshi candles into a leakage-free feature+label setup. Glue table `crypto_raw.kalshi_btc_15min` was created by owner in the Athena console (least-priv pipeline user can't `CreateTable` on `crypto_raw`); verify SELECT green.

**Models (full `dbt build` = PASS 36/36):**
- `staging/stg_kalshi_btc_15min` (view) — clean candles, 1 row per `(market_ticker, event_at)`; derive `mid_price` + `spread`; drop null-price rows. Tests: grain unique, not-nulls, `implied_prob` in 0..1.
- `intermediate/int_kalshi_implied_prob` (view) — **per wall-clock minute, the active market's implied prob**. PIT-critical filter `window_open <= event_at < window_close` (strict `<` picks the just-opened market at a boundary, not the settling one). **`unique event_at` test passes → exactly one active market per minute.**
- `marts/fct_features_pit` — added `kalshi_implied_prob/mid/spread`. Join correctness: Kalshi is BTC-only, so tag the CTE `asset_id='BTC-USD'` and join on `(asset_id, event_at)` — joining on `event_at` alone would wrongly give ETH rows BTC's prob. Value is the price AT T → PIT-safe. Full-refresh (schema change); **PIT test still PASS**. BTC: 86,626/100,076 minutes have prob (rest predate the 03-26 Kalshi launch), avg ≈ 0.505; ETH: 0 (NULL, expected).
- `marts/fct_kalshi_15min_label` (view, overrides marts incremental default) — **forward up/down label**, 1 row per settled window, `label_up = result=='yes'`. FORWARD-LOOKING → deliberately NOT in `fct_features_pit`; join at train time on `window_open_at = event_at`. Class balance **3,118 up / 3,103 down** (~50/50 → 15-min direction is near coin-flip; beating the market after costs is the real bar).
- `marts/fct_btc_15min_training` (view) — **the leakage-free trainable table**: one row per settled window = PIT features + `kalshi_implied_prob` (benchmark) + `label_up`, joined at `decision_at` = the first observable Kalshi minute (~W+1, since the window's market doesn't exist before W). **6,191 windows; sanity check: market avg implied prob 0.503 ≈ actual up-rate 0.502 → benchmark well-calibrated AND the join has no look-ahead (a leakage bug would break that); avg spread 0.012 (~1.2¢) = the cost hurdle.**

**Learned:** dbt-athena `accepted_values` on an INTEGER column errors (macro quotes values as strings; Trino is strict int-vs-varchar) → use `quote: false`.

**▶ PICK UP HERE NEXT TIME — the ML model (new session):**
1. ✅ DONE — `fct_btc_15min_training` is the leakage-free table (read it / UNLOAD to S3 Parquet for SageMaker).
2. **Walk-forward** model in `ml/` (lightgbm or sklearn; NO shuffled splits — time-ordered folds). Honest metric = beat the market-implied prior (`kalshi_implied_prob`), **net of `kalshi_spread`/fees**; report calibration + cost-aware PnL. Write predictions to a `fct_model_predictions` mart; run inference from an Airflow task (batch — see SageMaker note below).
3. Deployment (later, optional MLOps showcase): batch inference via Airflow is the project-fit default; a SageMaker **serverless** endpoint or **Pipelines** retrain-trigger is a strong resume add but NOT an always-on real-time endpoint (cost + overkill for a 15-min cadence).
4. (Optional) extend the PIT singular test to also recompute `kalshi_implied_prob` from raw at `<= T`.

**Context for a fresh chat:** branch `phase1/athena-pivot-and-ingestion`. New dbt models under `dbt/models/{staging,intermediate,marts}` (4 models + yml). `fct_features_pit` now carries the Kalshi feature; `fct_kalshi_15min_label` holds the label. Data: ~100k BTC feature-minutes + 6,221 settled labeled windows (~66 days, 2026-03-26 → now).

---

## 2026-06-01 — Kalshi 15-min BTC ingestion: public data, backfill + live DAG (both green)

**Big pivot (made the unit far simpler):** verified via docs that **Kalshi market-data endpoints — markets + candlesticks — are PUBLIC and unauthenticated** on prod (`https://external-api.kalshi.com/trade-api/v2`). We only need read-only price/implied-prob and never place orders, so **no API key / demo env / KYC is needed**. Also confirmed the demo env lacks real activity, so we use **prod market data, read-only** ("monitor price/action, no trades"). The RSA-PSS signer is still built in `ingestion/kalshi.py` but optional (public mode by default).

**Instrument discovered:** series **`KXBTC15M`** = market **"BTC price up in next 15 mins?"** (e.g. `KXBTC15M-26JUN011745-45`). Each settled market = one 15-min window; its 1-min candlesticks give everything the goal needs at once:
- `price.*_dollars` (0..1) = **implied probability** → benchmark + feature
- `yes_bid`/`yes_ask` = the **spread** = transaction cost (cost-aware PnL)
- market `result` (yes/no) = the up/down **label** (forward-looking → stays OUT of the PIT mart)

**Built + verified:**
- `ingestion/kalshi.py` — public/optional-auth client (retry+backoff, 0.5s pacing for the rate limit), `list_markets` (cursor pagination), `get_market_candlesticks`, `KalshiCandle` + `normalize_market_candles`. Public healthcheck `scripts/healthcheck_kalshi.py` green.
- `ingestion/kalshi_storage.py` — S3 Parquet writer, explicit schema = Glue DDL contract, one file per `dt` (window open date), idempotent overwrite. `ingestion/kalshi_backfill.py` — `backfill(days)` (settled history) + `ingest_current_day()` (re-fetch today, overwrite — the live contract).
- **Backfilled the FULL available history** (`KXBTC15M` launched 2026-03-26 — probed the ceiling) → **68 daily partitions, 101,053 candles (~4.5 MiB)** in `s3://.../raw/kalshi_btc_15min/dt=.../`, in ~4 min.
- **Live Airflow task** `ingest_kalshi_15m` added to `crypto_price_ingest` (parallel to the OHLCV mapped tasks). Added `cryptography` to `airflow/requirements.txt` + `KALSHI_API_BASE` to `airflow/.env`, rebuilt the image; **task ran green in-container**: `{'markets': 97, 'candles': 1432, 'files': 2}` written to S3.
- Glue external table DDL written to `docs/setup/05-kalshi-ingestion.md` with partition projection.

**Learned / gotchas:**
- The **Glue table is a USER action**: the least-priv `crypto-de-pipeline` user is read-only on `crypto_raw`, so `CREATE EXTERNAL TABLE crypto_raw.kalshi_btc_15min` fails with `AccessDenied: glue:CreateTable` — must be run with owner/admin (Athena console), same as `coinbase_ohlcv`. DDL is in `docs/setup/05`.
- Public API **rate-limits (429)** quickly → client paces 0.5s/call + exponential backoff.
- **Batch candlesticks endpoint** (`GET /markets/candlesticks`, ≤100 tickers, public) cut the full 6,320-market backfill from ~1 hr to ~4 min. Gotcha: its cap is on candlesticks **requested** = `n_markets × range_minutes` (max 10,000), NOT returned — a full-day range × 96 markets = 138,240 → 400. Fix: greedy **time-contiguous chunks** keeping `n × span ≤ 9000` (`_chunk_by_budget`), ~4 calls/day.
- Ceiling probe: `KXBTC15M` 15-min markets exist only back to **2026-03-26** (~66 days) — Kalshi history is the binding constraint on training/backtest depth, not Coinbase.
- During setup the demo key got pasted into `KALSHI_PRIVATE_KEY_PATH` (the field wants a *file path*) and a fragment surfaced in a tool error. Since we dropped auth, `.env` was cleaned to public config — **the unused demo key can be deleted in Kalshi** for hygiene.

**▶ PICK UP HERE NEXT TIME:**
1. **USER:** run the Glue DDL in `docs/setup/05-kalshi-ingestion.md` (Athena console, owner perms), then the verify `SELECT` should work as the pipeline user.
2. **dbt layer:** `stg_kalshi_btc_15min` (view over the raw table) → join the implied-prob as a **PIT-safe feature** into `fct_features_pit` (only data with `event_at <= T`); define the **forward 15-min up/down label** from `result` in a SEPARATE model (NOT in the PIT store).
3. Then the **model + walk-forward backtest** (Kalshi-benchmarked, net of spread/fees) and the Streamlit dashboard.

**Context for a fresh chat:** branch `phase1/athena-pivot-and-ingestion`. Kalshi work this session: new `ingestion/kalshi*.py`, `scripts/healthcheck_kalshi.py`, `docs/setup/05-kalshi-ingestion.md`, live task in `airflow/dags/crypto_price_ingest.py`, `cryptography` dep; `.env`/`.env.example` switched to public Kalshi config (`airflow/.env` gitignored holds `KALSHI_API_BASE`). 7 days of `kalshi_btc_15min` Parquet in S3. Local Airflow + Colima may still be running (`astro dev stop` + `colima stop` to shut down).

---

## 2026-06-01 — Resume-gap session: GitHub Actions CI (OIDC) + first runnable Airflow DAG

**Why this session (deliberate detour from the Kalshi plan):** a critical project review found the data layer (dbt + PIT + Iceberg) genuinely strong but the resume-legible pieces empty — Airflow, ML, dashboard, CI were all `.gitkeep` stubs while the README's resume bullets claimed Airflow DAGs, "40+ tests", CI, and a dashboard that don't exist. Chose to close the two highest-ratio gaps (CI + a real Airflow DAG) and fix the false claims, before resuming Kalshi.

**Did — CI (GitHub Actions, OIDC, no stored keys):**
- `.github/workflows/ci.yml` runs `dbt build` (compile → run → schema/PIT **tests**) against Athena on every push/PR. Concurrency-serialized per ref (Iceberg `MERGE` isn't concurrency-safe on one table).
- **AWS auth via GitHub OIDC federation** into a new role `crypto-de-ci` — the right call for a *public* repo (no long-lived keys anywhere). Wrote the role trust policy (`docs/setup/iam/github-oidc-trust-policy.json`), a CI S3 policy mirroring the user's inline S3 CRUD since a role can't inherit it (`ci-s3-access-policy.json`), and a step-by-step runbook (`docs/setup/04-github-oidc-ci.md`). The role reuses the existing athena-query + dbt-glue-write managed policies — same "grow perms deliberately" story, now on a federated role.
- `dbt deps` + `dbt parse` green locally → the project CI builds is structurally sound. **CI verified GREEN on GitHub** after the OIDC setup: role assumed via OIDC (no stored keys), `dbt build` + schema/PIT tests passed against Athena. (The first run failed as expected — pushed before the role existed; re-run after creating `crypto-de-ci` went green.)

**Did — Airflow (Astro Runtime 3 / Airflow 3, RUNS):**
- Stood up Colima (CLI-only Docker, no Docker Desktop) + Astro CLI; `astro dev init` in `airflow/`.
- `dags/crypto_price_ingest.py` — TaskFlow + **dynamic task mapping** over the product list (`ingest_product.expand(...)` → `summarize_ingest`). Ingestion imports are **lazy (inside the task)** so the DAG parses without the ingestion deps. Structure test in `tests/dags/`.
- **Ran it green end-to-end:** both mapped tasks (BTC-USD, ETH-USD) + summarize = success; **landed the `dt=2026-06-01` partition to real S3 — 1131 + 1131 = 2262 rows.** Verified the two Parquet files in the bucket + the task log line `crypto_price_ingest OK: 2262 rows`.
- **Caught a real trap:** `storage.py` writes one file per `(asset, day)`, overwrite. A naive "fetch last 15 min and write" would clobber the day partition down to 15 min. So the DAG **re-fetches the current UTC day (00:00→now) and overwrites** — idempotent, no data loss, matches the backfill contract.

**Learned (container wiring — the fiddly part):**
- The `ingestion/` package lives at the repo root, **outside the Astro Docker build context**, so it can't be `COPY`'d. Solution: **bind-mount `../ingestion`** into the scheduler (where LocalExecutor runs tasks) at `/usr/local/airflow/vendor/ingestion`, with `PYTHONPATH=/usr/local/airflow/vendor`.
- **Two different env mechanisms, easy to conflate:** (1) Astro injects the gitignored `airflow/.env` *into containers* (so PYTHONPATH + AWS creds reach the scheduler — env-var creds are the correct mechanism *inside* a container, distinct from the host where keys stay OUT of `.env`). (2) docker-compose `${VAR}` interpolation in the override file does **not** read `airflow/.env` → first start failed on `${INGESTION_SRC}` empty. Fix: a **relative** bind path (`../ingestion`), resolved against the project dir, no var needed.
- Colima gotcha: first `colima start` hung in VM provisioning even though the guest image had cached (under `~/Library/Caches/colima/caches/`, not `~/.lima`); `colima delete -f` + retry booted cleanly on the `vz` driver. Astro auto-appends `astro-run-dag` to requirements; Airflow 3 runtime is Python 3.13.

**Did — README honesty pass:**
- Added a **Project Status table** (built ✅ vs planned ⬜) as the top-of-README source of truth; rewrote the **resume bullets** to claim only what exists (killed the false Etherscan / "40+ tests" / two-DAGs / dashboard / walk-forward claims, split into "accurate today" vs "add once built"); flagged the stale on-chain + vol-nowcast sections and the BTC-directional/Kalshi pivot.

**▶ PICK UP HERE NEXT TIME:**
1. ✅ **DONE — AWS OIDC setup complete, CI green.** Created the OIDC provider, `crypto-de-ci` role (Web-identity trust scoped to `repo:derekkuang/data-engineering:*`), and 3 attached managed policies (`crypto-de-ci-s3`, `crypto-de-ci-athena`, `crypto-de-pipeline-dbt-glue-write` — note the Athena/Glue-read perms had to be re-created as a *managed* policy since the user's copy was inline and not role-attachable). Re-ran the workflow → green. NB: the repo is **private** (not public as first assumed) — OIDC still the right call.
2. **USER action (optional polish):** grab the Airflow UI screenshot for the README — stack runs at **http://airflow.localhost:6563** (Astro default login `admin`/`admin`); screenshot the green `crypto_price_ingest` grid. Shut down with `astro dev stop` + `colima stop` when done.
3. **Then resume the roadmap: Kalshi ingestion (Option B)** — the originally-planned next unit (RSA-PSS client, demo env, land `kalshi_btc_15min`, join implied-prob into the mart + define the forward label). See the prior two 2026-06-01 entries + `reference_kalshi_api.md`.

**Context for a fresh chat:** branch `phase1/athena-pivot-and-ingestion`. **This session's work is UNCOMMITTED** — new: `.github/workflows/ci.yml`, `docs/setup/iam/{github-oidc-trust-policy,ci-s3-access-policy}.json`, `docs/setup/04-github-oidc-ci.md`, the whole `airflow/` Astro project; modified: `README.md`. `airflow/.env` is gitignored (holds AWS keys for the container). Local Airflow stack + Colima may still be running.

---

## 2026-06-01 — CROWN JEWEL: fct_features_pit (Iceberg incremental) + PIT test, 12/12 green

**Did:**
- Built `models/marts/fct_features_pit.sql` — the point-in-time feature store, materialized as an **incremental Iceberg** table (`incremental_strategy='merge'`, `unique_key=['asset_id','event_at']`, `table_type='iceberg'`, `partitioned_by=['asset_id']`), with the `is_incremental()` watermark filter `event_at > (select max(event_at) from {{ this }})`. v1 is a thin assembler over `int_price_features` (Kalshi feature joins in later). Lands in Glue db `crypto_marts`.
- Added `macros/generate_schema_name.sql` — overrides dbt's default `<target>_<custom>` schema concat so `+schema: crypto_marts` lands in exactly `crypto_marts` (matches the IAM grant; default concat `crypto_staging_crypto_marts` would've been denied). `dbt_project.yml` marts block: `+materialized: incremental`, `+schema: crypto_marts`.
- **Demonstrated incremental live:** run 1 = `OK 180236` (full build, is_incremental false); run 2 immediately after = `OK 0` (watermark found nothing newer → merged 0 rows); row count held at 180,236 → merge idempotency proven (no dupes, grain test green). Cost scales with NEW data, not total.
- **Built the PIT singular test** `tests/assert_fct_features_pit_is_point_in_time.sql` — independently recomputes `rel_volume_20` for the 5 latest rows/asset using ONLY raw bars with `event_at <= T` (a backward-only join+rank), asserts equality with the stored feature. Returns 0 rows = pass.
- **Proved the test has TEETH** (it can fail): a 3-column inline (`stored` vs `backward_recompute` vs `forward_lookahead`) showed stored==backward exactly (3.905=3.905, 0.452=0.452…) while a forward-looking recompute diverged hard (3.905 vs 2.242, 0.452 vs 1.000) — i.e., if any window had peeked ahead, the test would surface rows and go red. Not a trivial green.
- Full suite: **`dbt test` = PASS 12/12** (staging/intermediate/mart grain+not-null + the PIT test).

**Learned (smaller):**
- dbt-athena writes a `view` with ZERO S3 data (Glue catalog object only) but an `incremental`/`iceberg` mart writes real Parquet+metadata to S3 — that's why the mart needed the Glue table/partition write IAM (already granted) and the views didn't.
- A passing test is only worth the proof that it CAN fail — always sanity-check teeth (the forward-vs-backward demo) before trusting a green PIT test.

**▶ PICK UP HERE NEXT TIME — Kalshi ingestion (Option B), now the next unit.** The price-only PIT store is done. Next: bring in the directional signal's other half.
1. New Python module `ingestion/kalshi.py` — RSA-PSS signed client (sign `timestamp+METHOD+path`, path sans query; headers KALSHI-ACCESS-{KEY,SIGNATURE,TIMESTAMP}), **develop against demo env `demo-api.kalshi.co`** first. Pull 15-min BTC up/down market **candlesticks** (1-min) → implied-prob history.
2. Land Parquet → `s3://.../raw/kalshi_btc_15min/dt=YYYY-MM-DD/`, mirror the storage.py contract pattern; Glue external table `crypto_raw.kalshi_btc_15min` (partition projection).
3. Then a `stg_kalshi_*` view + join the implied-prob feature into `fct_features_pit`; define the forward 15-min up/down **label** (separate, NOT in the PIT store).
4. See memory `reference_kalshi_api.md` for endpoints/auth and `project` memory for the trading/eval policy (edge-bet hold-to-expiry; metric = calibration + cost-aware PnL).

**Context for a fresh chat:** read this entry + the 3 memory files (project, collaboration, kalshi-api). All Phase-1 dbt work is COMMITTED on branch `phase1/athena-pivot-and-ingestion` (staging 9f88bfc, intermediate 35f7721, mart+PIT test cbd139a); working tree clean. To run dbt: `cd dbt && set -a && . ../.env && set +a && export DBT_PROFILES_DIR="$PWD"` then `uv run dbt run/test`. Next session starts on Kalshi ingestion (steps above).

---

## 2026-06-01 — Directional pivot (BTC 15-min + Kalshi) decided; int_price_features built

**Decided (the big one — a deliberate framing change):**
- **ML target is now BTC 15-minute up/down (directional)**, overriding the original "avoid directional, do volatility-nowcasting" framing rule. Reason it's defensible and not the naive-predictor trap: it's **anchored to Kalshi**, which runs liquid 15-min BTC up/down *binary* markets (~$70M/day) — so the market price IS an implied probability. Kalshi gives all three at once: the **benchmark** (beat the market-implied prior), a **tradable instrument** for a cost-aware backtest, and **exact 15-min horizon alignment**. The integrity guardrail is preserved, not dropped: walk-forward only, net of Kalshi spread/fees, benchmarked vs implied prob; honest bar = "beat the market after costs," not "profitable predictor." The label (sign of forward 15-min return) is forward-looking → stays OUT of `fct_features_pit`, joined only at train time.
- Verified Kalshi reality before committing (good instinct — it flipped two of my assumptions): 15-min BTC markets DO exist and are liquid; `GetMarketCandlesticks` (1/60/1440-min) makes historical backtest data fetchable; RSA-PSS auth; demo env at `demo-api.kalshi.co`. New raw source planned: `kalshi_btc_15min`. (Aside: CFTC approved Kalshi BTCPERP perp on 5/29 — brand new, ~no history, NOT the v1 instrument; the binary 15-min market is.) Captured in memory `reference_kalshi_api.md`.
- Scope calls: near-real-time **batch** via the planned 15-min Airflow DAG (live streaming deferred); Kalshi used as **benchmark + cost-aware trading backtest**.

**Did (modeling):**
- Built `models/intermediate/int_price_features.sql` (a **view**) — ~25 price/volume features per `(asset_id, event_at)` tuned for the directional target: multi-horizon log returns (1/5/15/60m), realized vol (rv 15/30/60m + short/long ratio), **range-based vol (Parkinson, Garman-Klass)** from the OHLC, ATR(14), **SMA-based RSI(14)** (chose SMA over recursive Wilder/EMA — negligible ML diff, SQL-clean), SMA-distance, Bollinger z-score, volume baselines (rel-volume, dollar-volume, signed-volume order-flow proxy), and PIT-safe calendar features (incl. sin/cos minute-of-day). Deferred rolling skew/kurtosis to v1.1 (tail/vol measures, little *directional* signal — start lean, add by feature importance).
- **The PIT rehearsal:** every rolling feature uses an EXPLICIT `rows between N preceding and current row` backward frame (never the default cumulative frame, never `following`), partitioned by `asset_id` so BTC windows never see ETH. This is the exact property the `fct_features_pit` crown-jewel test will later prove.
- Structured as layered CTEs (lags → per-bar building blocks → rolling aggregates → final ratios); used a named `WINDOW w` clause (Athena/Trino supports it). `dbt run`+`dbt test` green (PASS, grain unique + key not-nulls). Sanity-checked recent BTC rows: RSI in 0-100, tiny signed returns, positive vols. Note: feature cols are intentionally NOT `not_null`-tested — first ~60 bars/asset are warmup nulls by design.

**Learned (smaller):**
- `dbt show` appends its own LIMIT — pass `--limit N`, don't put `limit` in the inline SQL (double-LIMIT = parse error), and `--output` only takes `json`/`text`.
- Row-based frames assume contiguous minutes; BTC/ETH have ~0.7% missing bars, so "15 preceding rows" can span slightly >15 min. Accepted v1 simplification; documented the spine+forward-fill fix as future work.

**▶ PICK UP HERE NEXT TIME — decide next unit, then build:**
- **Option A (recommended): `fct_features_pit` mart** — Iceberg, incremental (`unique_key=['asset_id','event_at']`), built from `int_price_features` (price-only PIT store v1; Kalshi feature + label join in later). Then the **custom PIT singular test** (recompute a sample row from raw, assert equality) — the project's signature. This needs the dbt-glue-write policy's partition/Iceberg perms (already granted).
- **Option B: Kalshi ingestion** — new Python module (RSA-PSS auth, demo env first), land `kalshi_btc_15min` Parquet in S3, Glue external table, then join into the mart.
- Lean A first (delivers the crown jewel on data we already have), then B.

**Context for a fresh chat:** read this entry + the three memory files (project, collaboration, kalshi-api). dbt work on branch `phase1/athena-pivot-and-ingestion`; staging committed (9f88bfc), intermediate uncommitted.

---

## 2026-05-31 — dbt-athena stood up; first staging model (view) green end-to-end

**Did:**
- `uv add dbt-athena-community` (pulls dbt-core 1.11.11 + dbt-athena 1.10.1). First two `uv add` attempts no-op'd silently (flaky cache/network — "Resolved N packages", exit 0, but pyproject untouched); the third actually installed. Worth knowing: a 0 exit from `uv add` isn't proof the dep landed — verify pyproject/the venv.
- Scaffolded the dbt project under `dbt/`: `dbt_project.yml` (staging `+materialized: view` folder default), in-repo `profiles.yml` (env-var driven, **no secrets** — Athena auth rides the AWS credential chain; `database: awsdatacatalog` = catalog, `schema: crypto_staging` = Glue db dbt writes to), `packages.yml` (dbt_utils 1.3.3).
- **First model `stg_coinbase_ohlcv` as a VIEW** — decided view over incremental for staging: it's a thin rename/cast/dedupe with nothing expensive to amortize, always-fresh, zero stored copy, and Athena's column+partition pruning push *through* a view so it's near-free to re-run. Incremental/Iceberg is reserved for the marts where volume + compute justify the stateful machinery. Source declared in `_coinbase__sources.yml` (raw `crypto_raw.coinbase_ohlcv`); model renames open/high/low/close → `*_price`, drops impossible bars (null close, neg volume, high<low), dedupes to one row per `(asset_id, event_at)` via `row_number()`. Schema tests + dbt_utils grain-uniqueness test in `_stg_coinbase__models.yml`.
- **Verified:** `dbt run` green, `dbt test` PASS=5/5, and a `dbt show` count proves the view is exactly 1:1 with raw — **180,236 rows, 2 assets, 2026-03-24 → 05-26** (matches the 5/29 Athena healthcheck).

**Learned (the IAM iteration, as the spec predicted):**
- The 5/29 `athena-query` policy was deliberately **read-only on Glue**, so `dbt run` walked through a precise staircase of AccessDenied errors, each naming the next missing action: first `glue:CreateDatabase` (dbt auto-creates the model's schema db), then `glue:GetTableVersions` (dbt-athena's post-create version bookkeeping). Granted exactly those, scoped by resource ARN to **only `crypto_staging` + `crypto_marts`** — `crypto_raw` stays read-only to dbt. New artifact `docs/setup/iam/dbt-glue-write-policy.json`.
- **Inline-policy 2048-char wall.** Adding this as a *third inline* policy tripped IAM's "aggregate of all inline policies on a user ≤ 2048 non-whitespace chars" limit. Fix = make it a **customer-managed** policy (6144-char budget each, doesn't count against the inline aggregate, reusable, AWS-recommended). Editing a managed policy creates a new default version in place — no re-attach. Clean portfolio story: query-read and model-write are two separate, purpose-named policies, permissions grown exactly when a new capability needed them.
- A `dbt run` can mark a model ERROR on a *post-materialization* step (the `GetTableVersions` denial) while the view itself was already created — the tip-off was `dbt test` passing against the "failed" model. Errors aren't always all-or-nothing; read what step actually failed.

**▶ PICK UP HERE NEXT TIME — intermediate layer: `int_price_features.sql`.** Staging is green; next is feature computation per source. Concrete:
1. `models/intermediate/int_price_features.sql` over `{{ ref('stg_coinbase_ohlcv') }}` — per-`(asset_id, event_at)` features: 5/15/60-min returns, rolling realized volatility, RSI, Bollinger position. Compute with window functions **partitioned by `asset_id`, ordered by `event_at`** (never cross assets — discipline #9, no BTC-hardcoded logic). Likely `materialized: ephemeral` or `view`.
2. Watch for the **PIT trap** even here: every window must look *backward only* (`rows between N preceding and current row`), never `following` — that discipline is what the crown-jewel `fct_features_pit` test will later prove.
3. Then the mart `fct_features_pit` (Iceberg, incremental, `unique_key=['asset_id','event_at']`) + the custom recompute-from-raw singular test.

**Context for a fresh chat:** read this entry + the two memory files. dbt work is on branch `phase1/athena-pivot-and-ingestion`, not yet committed this session.

---

## 2026-05-29 — Athena warehouse stood up over the raw zone; healthcheck green

**Did:**
- Topped off the backfill (`--days 7`) before building the warehouse — overwrote the 5/22 partial-day partition and filled the 5/23–5/26 gap. Confirmed **idempotency-via-overwrite on real data**: re-running overlapping days rewrote identical files, no dupes, no watermark table needed.
- **Stood up the whole Athena layer** (`docs/setup/03-athena-s3.md`):
  - Athena **SQL** workgroup `crypto_wg` with a 1 GB per-query scan cutoff (cost guardrail) and results isolated in `athena-results/`, outside `raw/`.
  - Glue database `crypto_raw` + external table `coinbase_ohlcv` with **partition projection** on `dt` — no crawler, no `MSCK REPAIR`; Athena derives all 64 day-partitions from the S3 key pattern.
  - Authored the query IAM policy as a committed artifact (`docs/setup/iam/athena-query-policy.json`), least-privilege: Athena query lifecycle on `crypto_wg`, Glue **read-only** on `crypto_raw`, nothing more.
- Swapped the stale Snowflake block out of `.env` for the Athena vars; added `pyathena` (deferred `dbt-athena-community` to the dbt step).
- Wrote `scripts/healthcheck_athena.py`, mirroring the Coinbase one (staged OK/FAIL, 0/1 exit). It passes: **180,236 rows, 64 day-partitions, 2 assets, event_at 2026-03-24 → 05-26.**

**Learned (the bug worth remembering):**
- **Least-privilege bites in a precise, instructive way.** First healthcheck run failed at `SELECT 1` with `Unable to verify/create output bucket`. The ingestion policy granted object CRUD + `ListBucket`, so *writing* results worked — but Athena calls **`s3:GetBucketLocation`** (a *bucket-metadata* action, a different namespace from object actions) to verify the results bucket before every query, and that one action wasn't granted. Diagnosed precisely with two `aws s3api` probes (GetBucketLocation → AccessDenied; PutObject → OK), then added exactly that one action. Meta-lesson: object permissions and bucket-metadata permissions are separate in S3 IAM, and Athena needs both.
- Designing the healthcheck so **each stage exercises one IAM permission** (GetWorkGroup → glue:GetDatabase → StartQueryExecution → Glue table read) means a green run doubles as proof the policy is attached correctly — the healthcheck *found* the IAM gap instead of it surfacing as a runtime crash later.

**▶ PICK UP HERE NEXT TIME — stand up dbt-athena + first staging model.** The warehouse is live and healthchecked; the next sprint item is the modeling layer. Concrete steps:

1. `uv add dbt-athena-community` (pyathena is already in; dbt-core comes with the adapter).
2. Scaffold the dbt project: `dbt_project.yml` + `profiles.yml` (adapter `athena`, `work_group: crypto_wg`, `s3_staging_dir` = the `athena-results/` path, `schema`/staging db, `region_name: us-east-1`). Athena vars already live in `.env`.
3. `models/staging/_coinbase__sources.yml` — declare `crypto_raw.coinbase_ohlcv` as a dbt **source**.
4. `models/staging/stg_coinbase_ohlcv.sql` — thin **view**: rename/cast/standardize, 1:1 with source, no business logic. Add `not_null`/`unique`-style tests in the schema yml.
5. `dbt run` + `dbt test` — first model materialized and green.

This begins the medallion layer (staging → intermediate → marts) that leads to `fct_features_pit` (the PIT feature-store crown jewel) and its custom recompute-from-raw equality test. Decision to pause on when we get there: marts materialization = **Iceberg incremental** with `unique_key=['asset_id','event_at']` (the accepted Athena tradeoff). Reference: `docs/setup/03-athena-s3.md` Phase 5.

**Context for a fresh chat:** read this entry + `docs/setup/03-athena-s3.md` + the two memory files. Warehouse work from this session is committed on branch `phase1/athena-pivot-and-ingestion`.

---

## 2026-05-22 — Coinbase → S3 ingestion built; first real Parquet lands

**Did:**
- Built the `ingestion/` module, structured to map cleanly to the Airflow DAG tasks already sketched in the README:
  - `ingestion/coinbase.py` — API client. Paginates the 300-candle-max endpoint in 5-hour forward windows, normalizes Coinbase's `[time, low, high, open, close, volume]` LHOC rows into an `OhlcvBar` dataclass with UTC microsecond timestamps, dedupes on `event_at`, retries on 429/5xx with exponential backoff, sleeps ~0.15s between requests to stay under the public ~10 req/s ceiling.
  - `ingestion/storage.py` — S3 Parquet writer. Groups bars by `(asset_id, event_at.date())` and writes one file per partition to `s3://.../raw/coinbase_ohlcv/dt=YYYY-MM-DD/<asset>.parquet`. Uses an **explicit pyarrow schema** that's the literal contract with the Athena external-table DDL — `event_at` and `ingested_at` are `timestamp[us, tz=UTC]`, prices/volume are `double`. `dt` lives in the S3 key, not the file (so Athena's partition projection derives it from the path).
  - `ingestion/backfill.py` — CLI runner (`uv run python -m ingestion.backfill --products BTC-USD,ETH-USD --days 60`) with a `--dry-run` mode.
- Added deps: `pandas`, `pyarrow`, `boto3`. Added a focused mypy override (`ignore_missing_imports` for boto3/botocore/pyarrow/pandas) so strict mypy stays clean without dragging in the heavy `boto3-stubs`.
- **Validated end-to-end on real data.** Dry-run pulled 1438 bars for BTC-USD over 24h in 5 paginated requests, zero retries. A 1-day real run wrote two partition files to S3 (the 24h fetch correctly crossed UTC midnight into two `dt=` partitions, proving partitioning is by *event time*, not run time). Read the Parquet back via boto3 + pyarrow — schema matches the Athena DDL exactly.
- Kicked off the full 2-month BTC + ETH backfill — first real lakehouse data.

**Learned (the bug worth remembering):**
- **The boto3 env-var precedence footgun was real, and `.env.example` warned about it almost word-for-word.** First S3 write failed with `InvalidAccessKeyId` even though `aws sts get-caller-identity` worked. Cause: at some point `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` got added to `.env` with stale values; `load_dotenv()` injected them into the process env; boto3's credential chain prefers env vars over `~/.aws/credentials`; AWS CLI doesn't (it reads the shared file directly), so the two diverged. Fix: stripped the two lines from `.env`, kept a `.env.bak`, added `.env.bak` / `.env.backup` to `.gitignore`. The lesson is the meta-lesson: when a spec calls out a footgun, the spec is right — re-read it before debugging.

**Learned (smaller things):**
- Pinning Parquet to an **explicit pyarrow schema** instead of letting it be inferred is the right discipline for a lakehouse — it makes the file format the contract that the warehouse DDL has to match, not the other way around. Catches drift at write time.
- Coinbase's LHOC row order (time, low, high, open, close, volume) really is easy to get wrong; the constant index names (`_TIME, _LOW, _HIGH, _OPEN, _CLOSE, _VOLUME`) make it impossible to mis-index in code review.
- Idempotent backfill = "one file per (asset, day), overwrite on re-run." No state, no watermark table, no DELETE — just put_object. Simpler is genuinely better here.

**Next:** define the Glue external table + Athena workgroup over the real S3 partitions (`docs/setup/03-athena-s3.md` Phases 1–4), wire the IAM permissions to let `crypto-de-pipeline` query, write `scripts/healthcheck_athena.py`. After that, dbt-athena staging.

---

## 2026-05-22 — Warehouse pivot #2: Snowflake → Athena (all-AWS)

**Did:**
- Went to sign up for the Snowflake trial and hit a wall: the only signup on offer is now the **Cortex Code CLI** flow (`signup.snowflake.com/cortex-code`) — credit card required, $2 auth hold, and it **auto-converts to a $20/month subscription on ~June 21** unless cancelled. The old card-free 30-day / $400-credit trial that the whole 5/20 decision rested on is no longer available to me.
- Re-opened the warehouse decision rather than pay $20/mo for a portfolio warehouse. Re-evaluated the three coherent options:
  - **S3 + BigQuery (cross-cloud)** — rejected *again*; BigQuery can't cleanly query S3 (Omni is enterprise/region-limited), so it means egress + two IAM models — the exact mismatch I killed on 5/20.
  - **GCS + BigQuery (all-GCP)** — free-forever tier, best dbt fit, strong keyword, but abandons the S3 + IAM work and needs GCP re-setup. (The GCP project is soft-deleted, restorable until ~June 19 — so not from scratch.)
  - **S3 + Athena (all-AWS)** — **chosen.**
- **Decision: S3 + Athena.** Reasons: zero rework (reuses the existing bucket + `crypto-de-pipeline` IAM as-is), serverless pay-per-scan (~cents at this data volume), **no expiry clock** so the warehouse can stay live indefinitely for the Loom/dashboard, and it's the literal lakehouse pattern this architecture is built around — discipline #1 (Parquet-in-S3 as source of truth, queried in place) is *native* in Athena, not extra work. Phase-3 Spark stays all-AWS (Glue/EMR).
- Tradeoffs accepted: dbt *incremental* models are fiddlier on Athena → I'll use **Iceberg** table format for clean merge/incremental. Resume keyword is a notch below Snowflake/BigQuery, but "Athena/Glue lakehouse" reads as data-platform work, which matches the project's framing well.
- **Build order flipped.** With Snowflake I wanted the warehouse up first (to not waste the 30-day clock). Athena has no clock and just defines a table *over whatever Parquet is already in S3* — so the order is now **ingestion-first**: land Parquet in S3 → then point Glue/Athena at it.
- Synced docs: marked `02-snowflake-s3.md` SUPERSEDED, wrote `docs/setup/03-athena-s3.md`, updated the README stack/architecture/steps, swapped the Snowflake `.env` block for Athena vars.

**Learned:**
- Vendor "free trials" can disappear underneath a decision — Snowflake now funnels signups into the card-gated Cortex Code subscription; the bare `signup.snowflake.com` no-card trial is effectively gone.
- Athena is the most architecturally honest fit for a lakehouse: it queries Parquet in object storage in place, so "warehouse reads via external table, not COPY INTO" stops being a discipline I have to enforce and becomes the default.
- The cost model inverts too: Snowflake = time-boxed credits (warehouse-first to not waste them); Athena = pay-per-scan with no clock (data-first, warehouse is just a schema over files).

**Next:** build `ingestion/coinbase.py` — backfill BTC-USD + ETH-USD 1-min bars, write Parquet partitioned `dt=YYYY-MM-DD` under `s3://.../raw/coinbase_ohlcv/`, watermark-driven incremental. Then Glue/Athena tables over it, then dbt-athena staging.

---

## 2026-05-20 — GCP setup, warehouse pivot to Snowflake, docs

**Did:**
- Set up GCP/BigQuery end-to-end: project `crypto-de-portfolio` with billing, three datasets (`crypto_raw`/`staging`/`marts`), service account `crypto-de-sa` with least-privilege roles (`bigquery.jobUser` + `dataEditor`), a key file, and `scripts/healthcheck_bigquery.py` passing end-to-end.
- Stepped back and reviewed the storage/warehouse architecture. Realized landing in **S3 (AWS)** while querying in **BigQuery (GCP)** was a cross-cloud mismatch.
- Compared three coherent single-cloud options — **S3+Athena**, **GCS+BigQuery**, **S3+Snowflake** — across industry usage, downstream flexibility, cost, and resume value.
- **Decision: Snowflake on AWS, reading S3.** Reason: strongest resume keyword, canonical "Snowflake + dbt + Airflow + S3" stack, and it reuses the S3 + IAM work already done. Trade-off accepted: 30-day trial, not perma-free — fine because the portfolio lives in the repo + README + a Loom, not a live warehouse.
- Updated docs: marked the GCP runbook SUPERSEDED, wrote `docs/setup/02-snowflake-s3.md`, updated the README stack/architecture/steps. Started this devlog.
- **Tore down GCP** (deleted the service-account key + the project) now that the warehouse is settled — no orphaned credentials.

**Learned:**
- GCP IAM service accounts & roles, and how they map to AWS IAM concepts.
- Application Default Credentials (how the client auto-discovers a key via `GOOGLE_APPLICATION_CREDENTIALS`).
- The lakehouse idea: open Parquet in object storage decouples storage from compute, so the warehouse is a swappable component — and that's why storage and compute should live in the *same* cloud to avoid egress.
- Preview of Snowflake's storage-integration trust model (no keys stored in Snowflake; it assumes an AWS IAM role).

**Next:** sign up for the Snowflake trial (AWS, `us-east-1`), then build storage integration → external stage → healthcheck.

---

## 2026-05-12 — AWS landing zone

**Did:**
- Created IAM user `crypto-de-pipeline` with a least-privilege inline policy (single bucket; list/get/put/delete only).
- Created the S3 raw-landing bucket `derekkuang-crypto-de-raw-546712138633-us-east-1-an`.
- Ran `aws configure`; credentials live in `~/.aws/credentials`. Deliberately kept AWS keys *out* of `.env` to avoid the env-var-precedence footgun with boto3.
- Set up the uv project (Python 3.12); deps: `httpx`, `python-dotenv`, plus dev group (`ruff`, `mypy`, `pytest`).
- Wrote `scripts/healthcheck_coinbase.py` — verifies Coinbase API reachability, response schema, and data freshness; exit codes so it can wire into CI/Airflow.

**Learned:** AWS IAM least-privilege policies; why env vars override `~/.aws/credentials` in boto3.

---

## 2026-05-10 – 05-11 — Direction & scoping

**Did:**
- Chose the domain: crypto OHLCV + on-chain data (BTC-USD, ETH-USD, 1-min bars).
- Set the framing rule: this is a **data-platform** project, not a price predictor — the ML is a small demo.
- Committed a phased roadmap (Phase 1 OHLCV → Phase 2 scale to ~20 pairs → Phase 3 tick data + PySpark → Phase 4 fan-out) and the 10 architectural disciplines that keep Phase 3 viable from day 1.
