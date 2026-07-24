# `ml/` — research & trading code

Organized into three subpackages by **status**, so what's load-bearing is obvious.

Through-line of the whole effort: every *predictive / competed / toxic* edge came back
null; the one *structural* edge that survives is `lp/` — making Kalshi's wide retail
in-play sports spreads (and specifically soccer, whose rare discrete scoring keeps the
book mean-reverting).

## `lp/` — ACTIVE: live Kalshi market-making bot (real money)

Two-sided liquidity provision capturing the bid-ask spread on Kalshi in-play
TOTAL/SPREAD markets.

- **`lp_live.py`** — the live maker (places **REAL** orders). Staged safety ladder:
  `--auth-check` → `--test-order` → `--live --i-understand-live`. Levers: `--prefix KXWC`
  (restrict universe, e.g. World-Cup-only), `--ab` (randomize 1×/2× quote size for a clean
  size test). Strategy ruleset stamped via `CONFIG_VERSION`.
- **`lp_pilot.py`** — shared selection (`pick_smooth_ticker`, `better_market`) + risk caps
  + paper v2 simulator.
- **`lp_gate.py`** — selection gate (toxic-type exclusion + recent-trade floor).
- **`lp_analyze.py`** — read `data/lp_sessions.csv` + `lp_fills.csv`: P&L, markout CI,
  by-type / by-config breakdowns.
- `lp_paper_pilot.py`, `lp_market_screen.py`, `lp_toxicity_screen.py` — earlier paper run
  + the 2-stage opportunity/toxicity screens (history).
- **`realized_toxicity.py`** — GROUND TRUTH: realized per-family maker toxicity (30s fill-markout)
  from our own 15,829 WC fills, soccer-aware classifier, ET-day-block bootstrap. The load-bearing
  measurement the whole toxicity apparatus is validated against. Verdict: WC/SPREAD carries a mild
  but REAL adverse-selection tax (−0.135c) — the edge is capture *exceeding* toxicity, not benign flow.
- **`breakeven.py`** — STEP 0 go/no-go: does a candidate club league's near-money spread earn enough
  CAPTURE to clear that toxicity tax + fees? Fits `net_per_fill = capture + markout` vs quoted spread
  per market-type from the WC fills (day-block bootstrap), solves the breakeven spread, sign-checks on
  the known-toxic controls, and places each club league (LIVE `soccer_screen` spreads; 07-15 snapshot
  fallback) against the curve. Finding: spread width is NOT the binding constraint for club SPREAD
  (breaks even ~1c); the real gate is capture-efficiency + toxicity TRANSFER → needs a live club capture.
  Club TOTAL is marginal (WC/TOTAL itself barely cleared).
- **`soccer_screen.py`** — does the WC market-making microstructure repeat in year-round club
  soccer (MLS/Brasileirão/Liga MX/Scandinavia)? Screens near-money TOTAL/SPREAD spread + volume
  vs the WC benchmark. First read: club near-money spreads ARE in-band (Liga MX widest ~4c); the
  in-play flow/mean-reversion half still needs a live-game `ws_logger` run to confirm.
- **`edge_verdict.py`** — THE edge answer, per family: reads `fct_toxicity_by_family` (the
  captured flow-signed markout) + `fct_lp_market_session` (realized capture) and prints
  FLOW-TOXIC / FLOW-BENIGN / INCONCLUSIVE / INSUFFICIENT with day-block bootstrap CIs,
  split-half stability, known-toxic instrument controls (ITF/MLB-GAME must read toxic), and
  the multiple-comparison caveat. FLOW-BENIGN gates stage 2 (live bot); it is not itself edge.

The **DE pipeline** for this bot's data lives outside `ml/`:
`ingestion/lp_storage.py` → `dbt/models/{staging,marts}/*lp*` → `docs/setup/07-lp-pipeline.md`.

## `weather/` — OPEN: Kalshi weather DIRECTIONAL edge hunt + NOAA pipeline

The daily high-temp ladders are WC-scale volume (~1M contracts/day across the big cities).
*Making* them is dead (the convergence pick-off — see `research/weather_logger.py`); this
tests *taking*: can a forecast/observation model out-price the buckets by more than spread+fee?

- **`calib_study.py`** (W0) — fetches settled NYC/LAX ladders from the public API, samples the
  market's implied bucket probabilities at fixed decision times (evening-before → 6pm day-of),
  and runs the BTC-benchmark calibration harness (log-loss / Brier / ECE / reliability). First
  read: market is well-calibrated (ECE ~2–4%), with residual miscalibration concentrated in the
  **morning** window (ECE up to ~6.5%) — where a model edge (H1) would live, if any.
- Next (W1): `ingestion/noaa.py` + `weather_storage.py` — NOAA/NWS + Open-Meteo → S3 → dbt
  `fct_weather_pit`; then `edge_study.py` (W2) for the walk-forward, cost-aware verdict.

## `research/` — CLOSED side-tracks (kept for the narrative; all null/dead)

- `tennis_logger.py` / `tennis_analyze.py` — in-play tennis is a martingale at tradable
  horizons; overreaction-fade and momentum both null.
- `polymarket_navigator.py` / `poly_reward_logger.py` / `poly_reward_analyze.py` —
  Polymarket spread-capture (1c spreads) **and** reward-farming both DEAD (reward covers
  ~2% of the goal pick-off).
- `weather_logger.py` — Kalshi daily temp markets: maker *pays* ~0.44c on a 1c competed
  spread into a guaranteed end-of-day convergence pick-off; dead.
- `cross_venue_spike.py` / `binary_perp_basis.py` — Kalshi↔Polymarket arb + binary↔perp
  basis; both null.

## `alpha/` — CLOSED: the BTC 15-min directional alpha hunt (the narrative spine)

Proved, net of cost, that you can't beat Kalshi's 15-min BTC market (~10 axes, all null).
The platform was built to find edge and rigorously killed its own results.

- **foundation**: `data`, `model`, `walkforward`, `metrics`, `backtest`, `derivatives`,
  `orderflow`
- **experiments**: `benchmark_eda`, `train_baseline`, `favorite_longshot`, `settlement_lag`,
  `decision_minute_profit`, `live_exec_reconcile`, `live_paper_pnl`, `live_cluster_verdict`,
  `altcoin_efficiency`, `market_making`, `options_implied`, `threshold_arb`
