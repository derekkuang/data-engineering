"""Walk-forward model/feature comparison vs the Kalshi benchmark.

Runs each (model, feature-set) through the SAME leakage-free expanding-window split
and scores pooled out-of-fold predictions head-to-head against kalshi_implied_prob
on the same rows. The runs are designed as a controlled experiment:

  logistic        — interpretable baseline on public BTC features (the control)
  logistic+flow   — plus Binance taker order-flow features (does flow add signal?)
  lgbm+flow       — gradient boosting on BTC+flow (does a fancier model + data help?)
  logistic+fund   — plus Deribit funding features (does the slow derivative help?)

If neither augmented run beats plain logistic, those orthogonal sources carry no
15-min directional signal the market hasn't already priced — the recorded result.
Market-derived columns stay OUT of the features (INCLUDE_MARKET).

Usage:
    uv run python -m strategies.btc_direction.train_baseline
"""

import sys
import warnings

import numpy as np
import numpy.typing as npt

from core.backtest.data import BENCHMARK_COL, TARGET_COL, feature_matrix, load_training_frame
from core.backtest.derivatives import add_funding_features
from core.backtest.metrics import reliability_table, score
from core.backtest.model import (
    EstimatorFactory,
    lightgbm_pipeline,
    logistic_pipeline,
    walk_forward_oof,
)
from core.backtest.orderflow import add_orderflow_features

N_SPLITS = 8
INCLUDE_MARKET = False  # keep market-derived cols (incl. kalshi_mid_price) OUT
# add "log_return_1m" to stress-test the W->W+1 price head start (by NAME only — the
# flow/funding features carry the same head start and would need adding here too).
DROP_FEATURES: list[str] = []


def _print_comparison(scores: dict[str, dict[str, float]], market: dict[str, float]) -> None:
    """One row per metric; one column per run, plus the market. For log_loss /
    brier / ece lower is better; for accuracy higher is better."""
    names = list(scores)
    w = 14
    print(f"{'metric':<10}" + "".join(f"{n:>{w}}" for n in names) + f"{'market':>{w}}")
    print("-" * (10 + w * (len(names) + 1)))
    for key in ("log_loss", "brier", "accuracy", "ece"):
        cells = "".join(f"{scores[n][key]:>{w}.4f}" for n in names)
        print(f"{key:<10}{cells}{market[key]:>{w}.4f}")


def main() -> int:
    # Benign: numpy arrays have no column names; sklearn warns once per fold on
    # LightGBM predict. Predictions match by position regardless.
    warnings.filterwarnings("ignore", message="X does not have valid feature names")

    df = load_training_frame()
    n = len(df)
    if n == 0:
        print("FAIL: fct_btc_15min_training returned 0 rows.", file=sys.stderr)
        return 1

    y = df[TARGET_COL].astype(int).to_numpy()
    market = df[BENCHMARK_COL].astype(float).to_numpy()
    drop = tuple(DROP_FEATURES)
    x_base, feats_base = feature_matrix(df, include_market=INCLUDE_MARKET, drop=drop)

    # control: BTC features only
    runs: list[tuple[str, EstimatorFactory, npt.NDArray[np.float64]]] = [
        ("logistic", logistic_pipeline, x_base),
    ]

    # add Binance order-flow features (if the cache exists) — the orthogonal-info test
    try:
        df_flow = add_orderflow_features(df)
        x_flow, feats_flow = feature_matrix(df_flow, include_market=INCLUDE_MARKET, drop=drop)
        runs += [
            ("logistic+flow", logistic_pipeline, x_flow),
            ("lgbm+flow", lightgbm_pipeline, x_flow),
        ]
        print(f"Order-flow features added: +{len(feats_flow) - len(feats_base)} "
              f"({len(feats_base)} BTC -> {len(feats_flow)} total)")
    except FileNotFoundError:
        print("No flow cache — run `uv run python -m ingestion.binance_flow ...`; skipping flow")

    # add Deribit funding features (if the cache exists) — second orthogonal test
    try:
        df_fund = add_funding_features(df)
        x_fund, feats_fund = feature_matrix(df_fund, include_market=INCLUDE_MARKET, drop=drop)
        runs.append(("logistic+fund", logistic_pipeline, x_fund))
        print(f"Funding features added:    +{len(feats_fund) - len(feats_base)} "
              f"({len(feats_base)} BTC -> {len(feats_fund)} total)")
    except FileNotFoundError:
        print("No funding cache — run `uv run python -m ingestion.deribit ...`; skipping funding")

    print(f"Rows: {n:,}   folds: {N_SPLITS}   market in features: {INCLUDE_MARKET}\n")

    oof = {name: walk_forward_oof(x, y, factory, N_SPLITS) for name, factory, x in runs}
    scored = ~np.isnan(next(iter(oof.values())))
    y_eval, market_p = y[scored], market[scored]
    print(f"Scored {int(scored.sum()):,} out-of-fold windows (the held-out tail).\n")

    scores = {name: score(y_eval, preds[scored]) for name, preds in oof.items()}
    _print_comparison(scores, score(y_eval, market_p))

    best = "logistic+flow" if "logistic+flow" in oof else "logistic"
    print(f"\n{best} reliability (calibration) table:")
    print(reliability_table(y_eval, oof[best][scored]).to_string(index=False))

    print("\nComparison complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
