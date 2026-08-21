# btc_direction — the BTC 15-min directional alpha hunt

**Status: CLOSED. ~10 axes, all null net of cost. The narrative spine of the platform.**

**Benchmark.** Kalshi's KXBTC15M market is near-perfectly calibrated (ECE 0.5%); its
log-loss 0.659 is the bar. A clean walk-forward logistic **ties** it — nothing beats it.

**What was killed (each file = one axis, all leakage-safe + cost-aware via
`core/backtest/*`):** baseline features, order-flow (53.3M Binance trades → OFI: 0.655→0.656),
Deribit funding/derivatives, options-implied direction (null by construction at 15 min),
favorite-longshot bias (absent — tails priced fairly), settlement-lag / decision-minute
displacement, altcoin (ETH/HYPE) efficiency, threshold-ladder static arb (0 executable net
of fees), market-making the BTC book (sub-minute toxicity), live cluster W+9–13 (selection
noise, every leg negative on live data).

**The +8% saga.** The one "edge" the backtest ever showed collapsed on live-execution
reconciliation — a latency-bound lead-lag artifact, not capturable alpha
(`live_exec_reconcile.py`, `scripts/measure_execution_latency.py`).

**Verdict.** 15-min BTC direction is efficiently priced w.r.t. reasonable public info.
The platform's job was to find edge and it rigorously killed its own results.

Foundation modules now live in `core/backtest/`. Data: `fct_btc_15min_training`,
`fct_features_pit`, `fct_kalshi_15min_label`. Full story: devlog + memory
`project_benchmark_eda_finding`.
