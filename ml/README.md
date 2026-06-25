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

The **DE pipeline** for this bot's data lives outside `ml/`:
`ingestion/lp_storage.py` → `dbt/models/{staging,marts}/*lp*` → `docs/setup/07-lp-pipeline.md`.

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
