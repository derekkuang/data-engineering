"""Benchmark-first EDA: how good is Kalshi's own implied probability already?

Before training anything, establish the BAR. Each row of fct_btc_15min_training
carries kalshi_implied_prob — the market's P(up) at the decision minute — right
next to label_up, the realised outcome. A liquid betting market is a
self-interested forecaster; its implied prob is the baseline any model has to
beat. This scores it with proper scoring rules (core.backtest.metrics) and a reliability
table, against the trivial no-skill baselines.

Run this, read the numbers, THEN decide whether and where to train a model.

Usage:
    uv run python -m strategies.btc_direction.benchmark_eda
"""

import sys

import numpy as np

from core.backtest.data import BENCHMARK_COL, TARGET_COL, load_training_frame
from core.backtest.metrics import reliability_table, score


def main() -> int:
    df = load_training_frame()
    n = len(df)
    if n == 0:
        print(
            "FAIL: fct_btc_15min_training returned 0 rows — has the mart been built "
            "(dbt build) and has Kalshi/Coinbase ingestion run?",
            file=sys.stderr,
        )
        return 1

    y = df[TARGET_COL].astype(int).to_numpy()
    p = df[BENCHMARK_COL].astype(float).to_numpy()
    base_rate = float(y.mean())

    print(f"Rows: {n:,}   windows {df['window_open_at'].min()} .. {df['window_open_at'].max()}")
    print(
        f"Base rate P(up): {base_rate:.4f}  (up={int(y.sum()):,}  down={int(n - y.sum()):,})"
    )
    print()

    # The market vs two no-skill baselines, scored identically.
    market = score(y, p)
    base = score(y, np.full(n, base_rate))
    half = score(y, np.full(n, 0.5))

    print("Log loss (lower is better):")
    print(f"  Kalshi implied_prob : {market['log_loss']:.4f}")
    print(f"  predict base rate   : {base['log_loss']:.4f}")
    print(f"  predict 0.5         : {half['log_loss']:.4f}")
    print()
    print("Brier score (lower is better):")
    print(f"  Kalshi implied_prob : {market['brier']:.4f}")
    print(f"  predict base rate   : {base['brier']:.4f}")
    print()
    print(f"Directional accuracy @0.5 threshold: {market['accuracy']:.4f}")
    print()

    print("Reliability (calibration) table:")
    print(reliability_table(y, p).to_string(index=False))
    print(f"\nExpected calibration error (ECE): {market['ece']:.4f}")

    print("\nEDA complete — read the bar above before choosing a model/training path.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
