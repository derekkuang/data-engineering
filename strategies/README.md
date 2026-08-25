# `strategies/` — the edge map

One folder per strategy. Each folder = the strategy-specific code (screens, calibration
studies, one-off probes) + a `VERDICT.md` with the status, the numbers, and why it's
open or closed. The reusable machinery (maker engine, ws capture, walk-forward harness)
lives in `core/`; the data pipeline in `ingestion/` + `dbt/`.

## Status board (updated 2026-08-21)

| Strategy | Status | One-line verdict |
|---|---|---|
| [`soccer_mm/`](soccer_mm/VERDICT.md) | **ACTIVE** | The one surviving edge: in-play soccer spread-making, WC net +$53/5d; next = live Liga MX SPREAD pilot (capture-efficiency + toxicity transfer). |
| [`politics_mm/`](politics_mm/VERDICT.md) | **GATED** | First gross-positive of the whole hunt (maker@bid +3.4–7.3%/ct); paper says short-horizon toxicity non-fatal, but fill-rate + months-long inventory are capital-gated → needs a small real-money pilot (Derek's call). |
| [`btc_direction/`](btc_direction/VERDICT.md) | CLOSED | ~10 axes, all null net of cost; the 15-min market is calibrated (ECE 0.5%) and the +8% backtest was a latency artifact. |
| [`weather_taker/`](weather_taker/VERDICT.md) | CLOSED | Market well-calibrated (ECE 2–4%); no taker edge clears spread+fee; maker angle dead (−0.44c convergence pick-off). |
| [`tennis/`](tennis/VERDICT.md) | CLOSED | In-play tennis is a martingale at tradable horizons; also jump-TOXIC for makers. |
| [`pm_ladder_consistency/`](pm_ladder_consistency/VERDICT.md) | CLOSED | Polymarket NegRisk basket-arb SEALED (null) — median Σask 1.020, 0 buy-arbs; lit confirms bot-saturation (~0.08 USDC/conv, ~16s windows) + venue no longer zero-fee + QCX has no API. See [pm structural-edge research](../docs/research/polymarket_structural_edge_2026.md). |
| [`polymarket/`](polymarket/VERDICT.md) | CLOSED | 1c spreads kill capture; rewards cover ~2% of goal pick-off; the edge is Kalshi-retail-specific. |
| [`cross_venue/`](cross_venue/VERDICT.md) | CLOSED | Kalshi↔Polymarket race, binary↔perp basis, PM-vs-sportsbook divergence — all null. |
| [`parlays/`](parlays/VERDICT.md) | CLOSED | MVE parlay bias is real (~1.4×) but structurally uncapturable (buy-only book). |

Deep narrative (newest-first): `docs/devlog.md`. Literature:
`docs/research/prediction_market_literature.md`.

## The reusable workflow (how a new strategy gets from idea → verdict)

Every strategy walks the same ladder; each rung gates the next and rungs 1–4 cost $0.

1. **Screen** — where does the prize live? Exchange-wide scan via `ingestion/kalshi_universe`
   + `fct_kalshi_opportunity`, or a standalone probe (`core/maker/lp_market_screen`).
2. **Capture** — collect the deciding microstructure: `core/capture/ws_features` (add the
   prefix to `ws-capture.yml`) → `fct_ws_markout` / `fct_toxicity_by_family`.
3. **Study** — the strategy-specific calibration/edge analysis, in this folder. Directional
   ideas reuse `core/backtest/*`; maker ideas reuse the toxicity marts.
4. **Paper** — `core/maker/lp_paper_pilot` (zero-money, schedulable as a GH workflow) for
   maker strategies; walk-forward + cost model for taker strategies.
5. **Verdict** — write `VERDICT.md` with day/event-block bootstrap CIs. Maker families flow
   into `core/maker/edge_verdict --emit` so the live bot's fail-CLOSED gate knows about them.
6. **Live pilot** — only on a CONFIRMED (or explicitly `--pilot`-overridden) family, real
   money, Derek present. Runbook pattern: `docs/setup/10-club-soccer-pilot.md`.

Rules of the house: resample by DAY/event (the honest sample is tens of days, not thousands
of windows); bootstrap CIs + split-half stability on any positive; state PROVEN vs ASSUMED;
never conclude "can't" without measuring.
