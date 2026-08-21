# `core/` — reusable engines (no strategy opinions)

Everything here is strategy-agnostic machinery. A strategy (see `strategies/`) is a thin
layer that points these engines at a slice of the exchange and owns its own verdict.

## `maker/` — the market-making engine (live, paper, gates, verdicts)

Two-sided liquidity provision on Kalshi. Used so far by `strategies/soccer_mm` (real money)
and `strategies/politics_mm` (paper).

- **`lp_live.py`** — the live maker (places **REAL** orders). Staged safety ladder:
  `--auth-check` → `--test-order` → `--live --i-understand-live`. Levers: `--prefix`
  (restrict universe), `--pilot FAMILY` (audited override of the family gate), `--ab`
  (randomized quote-size test). Ruleset stamped via `CONFIG_VERSION`.
- **`lp_pilot.py`** — shared selection (`pick_smooth_ticker`, `better_market`) + risk caps
  + the paper v2 simulator.
- **`lp_paper_pilot.py`** — zero-money paper maker; single-market and pooled multi-market
  (`--markets N`) modes; drives the autonomous paper-pilot workflows.
- **`classify.py`** — THE canonical ticker→(sport, market_type) taxonomy, single source of
  truth. Python callers import `sport()`/`market_type()`; dbt uses the macro it GENERATES
  (`--emit-sql` → `dbt/macros/classify.sql`); a parity test guards the two from drifting.
- **`quotable.py`** — fail-CLOSED family gate. Loads `quotable_families.json` (from
  `edge_verdict --emit`); quotes only freshly CONFIRMED families (or explicit `--pilot`).
  Missing/stale file → refuse all.
- **`lp_gate.py`** — selection gate (fail-closed family policy + toxic-type exclusion +
  recent-trade floor); `passes_gate` is the ONE enforcement point both selection paths use.
- **`edge_verdict.py`** — THE per-family edge answer: reads `fct_toxicity_by_family` +
  `fct_lp_market_session`, judges the **flow** axis (signed markout) and the **jump** axis
  (flow-independent pick-off) with day-block bootstrap CIs; `--emit` writes the
  freshness-stamped quotable tier (CONFIRMED / CANDIDATE / CONTRADICTION).
- **`realized_toxicity.py`** — GROUND TRUTH: realized per-family maker toxicity (30s
  fill-markout) from our own 15,829 WC fills; the measurement the whole toxicity apparatus
  is validated against.
- **`pickoff_dynamics.py`** — the pick-off risk rule: the jump is warn-able from the TAPE
  (trade-rate/flow/vol AUC ~0.8) but is a PULL signal, not a lean (direction ~coin-flip);
  book imbalance does NOT warn.
- **`lp_analyze.py`** — session/fill P&L, markout CIs, by-type/by-config breakdowns from
  `data/lp_sessions.csv` + `lp_fills.csv`.
- `lp_market_screen.py` / `lp_toxicity_screen.py` — the 2-stage exchange-wide
  opportunity/toxicity screens (where does the spread prize live; which flow is toxic).

## `capture/` — in-play websocket capture (the toxicity data layer)

- **`ws_features.py`** — the scheduled capture entrypoint (`ws-capture.yml`, 4×/day):
  live tape + book features per market → S3 → `stg_ws_features` → `fct_ws_markout` /
  `fct_toxicity_by_family`.
- `ws_logger.py` / `ws_book_feed.py` — the underlying tape/book websocket plumbing.

## `backtest/` — the directional-research harness (walk-forward, cost-aware)

Built for the BTC hunt, reusable for any "can a model out-price this market?" question.

- `data.py` (Athena→pandas loader) · `model.py` (walk-forward OOF logistic/LightGBM) ·
  `walkforward.py` (expanding-window splits — never shuffle) · `metrics.py` (log loss /
  Brier / ECE / reliability) · `backtest.py` (cost model, effective quotes, bet rules) ·
  `derivatives.py` / `orderflow.py` (feature builders).

**Discipline** (applies to every consumer): resample by DAY/event, honest unit = days not
rows; bootstrap CIs on every ROI; separate PROVEN from ASSUMED in verdicts.

The **data pipeline** these engines sit on lives outside `core/`:
`ingestion/` (Kalshi REST/WS + Coinbase/Binance/Deribit + storage writers) →
S3/Glue → Athena → `dbt/` (staging → marts). See `docs/setup/`.
