"""Forward paper-trading PnL — the out-of-sample live-execution test.

The reconciliation (ml/live_exec_reconcile.py) and the latency gate
(scripts/measure_execution_latency.py) established: the +8% backtest price was
real at the instant, and a ~0.6s decision->order loop sits ~50x inside the ~30s
breakeven. The only thing left that offline backtesting can't prove is whether the
edge holds OUT-OF-SAMPLE when you (paper-)trade it forward at the REAL executable
price. This is that test.

Design (leakage-safe, reuses the platform rather than re-porting dbt features):
  * CAPTURE (live, already running): the launchd collector logs the live executable
    touch at each decision minute to data/orderbook_snapshots.jsonl. That live quote
    is the only irreproducible thing.
  * SCORE (batch, here): once a captured window has SETTLED and the daily pipeline
    has landed its decision-minute features in fct_btc_15min_training, fit the
    production logistic on every window STRICTLY BEFORE the captured block (a true
    out-of-sample / forward fit), score the captured windows, and tally paper PnL by
    the same bet rule + cost model as the backtest — but filling at the LIVE touch,
    not the backfill candle. The backfill-price PnL is shown alongside as the
    "what the backtest assumed" reference.

It is FORWARD by construction: it can only score windows the collector captured
AND that have since settled into the warehouse, so the readout grows each day as
the collector + batch pipeline accumulate windows. With few windows it is a wiring
proof; the statistically meaningful verdict accrues over days.

Usage:
    uv run python -m strategies.btc_direction.live_paper_pnl
"""

import sys
from datetime import timedelta

import numpy as np
import pandas as pd

from core.backtest.backtest import _summarise
from core.backtest.data import BENCHMARK_COL, TARGET_COL, feature_matrix, load_training_frame
from core.backtest.model import logistic_pipeline
from strategies.btc_direction.live_exec_reconcile import (
    DECISION_OFFSET_S,
    _burst,
    _iso,
    _load_windows,
)


def _live_touch_by_window() -> dict[pd.Timestamp, dict[str, float]]:
    """Map each captured window's open time -> its live executable touch at the
    decision instant (the snapshot nearest W+1:00)."""
    out: dict[pd.Timestamp, dict[str, float]] = {}
    for snaps in _load_windows().values():
        window_open = _iso(snaps[0]["window_open_at"])
        burst = _burst(snaps, window_open)
        if not burst:
            continue
        target = window_open + timedelta(seconds=DECISION_OFFSET_S)
        dec = min(burst, key=lambda s: abs((_iso(s["captured_at"]) - target).total_seconds()))
        if dec.get("best_yes_ask") is None or dec.get("best_yes_bid") is None:
            continue
        key = pd.Timestamp(window_open).tz_convert("UTC")
        out[key] = {
            "yes_ask": float(dec["best_yes_ask"]),
            "yes_bid": float(dec["best_yes_bid"]),
            "mid": float(dec["mid"]),
        }
    return out


def main() -> int:
    df = load_training_frame()
    if len(df) == 0:
        print("FAIL: fct_btc_15min_training returned 0 rows.", file=sys.stderr)
        return 1

    live = _live_touch_by_window()
    if not live:
        print("No usable live touches in data/orderbook_snapshots.jsonl yet.", file=sys.stderr)
        return 1

    # Test set = captured windows that have since settled into the warehouse.
    df["window_open_at"] = pd.to_datetime(df["window_open_at"], utc=True)
    in_live = df["window_open_at"].isin(live.keys())
    n_test = int(in_live.sum())
    print(f"Captured live windows: {len(live)}   of which settled in warehouse: {n_test}")
    if n_test == 0:
        latest = df["window_open_at"].max()
        print(
            f"None of the captured windows are in the warehouse yet "
            f"(latest warehouse window: {latest}).\n"
            "The batch pipeline is still catching up — re-run once it lands today's data."
        )
        return 0

    # Out-of-sample fit: train on every window strictly before the captured block.
    first_test = df.loc[in_live, "window_open_at"].min()
    train_mask = (df["window_open_at"] < first_test).to_numpy()
    test_mask = in_live.to_numpy()
    x, feats = feature_matrix(df)
    y = df[TARGET_COL].astype(int).to_numpy()

    model = logistic_pipeline()
    model.fit(x[train_mask], y[train_mask])
    prob = model.predict_proba(x[test_mask])[:, 1]

    test = df.loc[test_mask].reset_index(drop=True)
    outcome = test[TARGET_COL].astype(int).to_numpy().astype(np.intp)
    implied = test[BENCHMARK_COL].astype(float).to_numpy()

    # Backfill touch (what the backtest assumed) from the decision-minute candle.
    mid_bf = test["kalshi_mid_price"].astype(float).to_numpy()
    spread_bf = test["kalshi_spread"].astype(float).to_numpy()
    yes_ask_bf = np.clip(mid_bf + spread_bf / 2.0, 0.0, 1.0)
    yes_bid_bf = np.clip(mid_bf - spread_bf / 2.0, 0.0, 1.0)
    no_ask_bf = 1.0 - yes_bid_bf

    # Live touch (the paper fill) aligned to the same windows.
    opens = test["window_open_at"].to_numpy()
    yes_ask_lv = np.array([live[pd.Timestamp(o)]["yes_ask"] for o in opens])
    yes_bid_lv = np.array([live[pd.Timestamp(o)]["yes_bid"] for o in opens])
    no_ask_lv = 1.0 - yes_bid_lv

    print(
        f"Train windows: {int(train_mask.sum()):,}   features: {len(feats)}   "
        f"test up-rate: {outcome.mean():.3f}\n"
        f"mean model prob: {prob.mean():.3f}   mean market implied: {implied.mean():.3f}\n"
    )

    # Same bet rule as the backtest (threshold 0), priced two ways.
    print(f"{'fill price':<24}{'bets':>8}{'win%':>9}{'PnL($)':>11}{'ROI':>9}")
    print("-" * 61)
    for label, ya, yb, na in (
        ("backfill candle (assumed)", yes_ask_bf, yes_bid_bf, no_ask_bf),
        ("LIVE touch (paper fill)", yes_ask_lv, yes_bid_lv, no_ask_lv),
    ):
        r = _summarise((prob - ya) > 0.0, (yb - prob) > 0.0, outcome, ya, na)
        print(
            f"{label:<24}{int(r['n_bets']):>8,}{r['win_rate']:>9.1%}"
            f"{r['pnl']:>+11.2f}{r['roi']:>+9.2%}"
        )

    if n_test < 30:
        print(
            f"\nNote: only {n_test} settled windows so far — this is a WIRING PROOF, not a "
            "verdict.\nRe-run as the collector + batch pipeline accumulate windows over the "
            "coming days."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
