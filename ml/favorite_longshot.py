"""Favorite-longshot test — is there a price-shape edge, independent of forecasting?

The direction-forecasting alpha hunt is closed: the only edge is a thin,
latency-bound microstructure effect (see ml/live_exec_reconcile.py). This asks a
DIFFERENT question that needs no faster execution and no better forecast — only
the market's own price vs. what actually happened:

  In nearly every studied betting market (horses, sports, election markets),
  bettors OVERPAY for longshots and UNDERPAY for favorites — the "favorite-longshot
  bias". If KXBTC15M shares it, then deep favorites are systematically underpriced:
  betting them blindly would win MORE often than their price implies, by enough to
  clear the spread. That is a structural, side-of-the-book edge, not a forecast.

Two parts:
  1. CALIBRATION by implied-prob bin — realized up-rate vs the price, with a
     standard error so we can tell a real S-shaped bias from sampling noise. (The
     benchmark EDA already found the market well-calibrated overall, ECE ~0.5%, so
     the prior is "no bias" — this looks specifically at the tails.)
  2. COST-AWARE PnL — bet the favorite (or the longshot) only when its price clears
     a threshold, net of the real spread + Kalshi fee, reusing the backtest's PnL
     accounting. If any tail is net profitable, the bias is tradeable.

Uses the same leakage-free table and cost model as the rest of ml/ — the price and
spread are measured at the decision minute, the label settles ~14 min later.

Usage:
    uv run python -m ml.favorite_longshot
"""

import sys

import numpy as np
import numpy.typing as npt

from ml.backtest import _summarise
from ml.data import BENCHMARK_COL, TARGET_COL, load_training_frame
from ml.metrics import reliability_table

FloatArr = npt.NDArray[np.float64]
IntArr = npt.NDArray[np.intp]

FAVORITE_THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90)
LONGSHOT_THRESHOLDS = (0.50, 0.40, 0.30, 0.20, 0.10)


def _print_calibration(y: IntArr, p: FloatArr) -> None:
    """Reliability table + standard error: does the realized up-rate match the
    price in each bin? A favorite-longshot bias shows as obs > pred in the high
    bins (favorites underpriced) and obs < pred in the low bins (longshots
    overpriced). |z| > 2 flags a gap unlikely to be noise."""
    table = reliability_table(y, p)
    print(f"{'bin':<12}{'n':>7}{'price':>9}{'realized':>10}{'gap':>8}{'z':>7}")
    print("-" * 53)
    for _, r in table.iterrows():
        n, obs = int(r["n"]), float(r["obs_freq"])
        se = float(np.sqrt(obs * (1.0 - obs) / n)) if n else float("nan")
        # gap = price - realized; z tests realized vs price (the bettable direction).
        z = (float(r["pred_mean"]) - obs) / se if se > 0 else float("nan")
        print(
            f"{r['bin']:<12}{n:>7,}{r['pred_mean']:>9.3f}{obs:>10.3f}"
            f"{float(r['gap']):>+8.3f}{z:>+7.1f}"
        )
    print("\n  gap = price - realized.  +gap in a HIGH bin = favorite UNDERPRICED")
    print("  (bettable); -gap in a LOW bin = longshot OVERPRICED (fade it).")


def _print_strategy_sweep(
    label: str,
    thresholds: tuple[float, ...],
    side_prob: FloatArr,
    bet_yes_when: npt.NDArray[np.bool_],
    outcome: IntArr,
    yes_ask: FloatArr,
    no_ask: FloatArr,
    favorite: bool,
) -> None:
    """Sweep a price threshold and tally cost-aware PnL for betting one side of the
    book. `side_prob` is the price of the side being bet; for favorites we require
    it to EXCEED the threshold (deeper favorite), for longshots to fall BELOW it
    (deeper longshot). `bet_yes_when` marks the windows where the chosen side is YES."""
    print(f"\n{label}")
    print(f"{'price cutoff':<14}{'bets':>8}{'bet%':>9}{'win%':>9}{'PnL($)':>12}{'ROI':>9}")
    print("-" * 61)
    for thr in thresholds:
        qualify = (side_prob > thr) if favorite else (side_prob < thr)
        bet_yes = qualify & bet_yes_when
        bet_no = qualify & ~bet_yes_when
        cutoff = f"{'>=' if favorite else '<='} {thr:.2f}"
        r = _summarise(bet_yes, bet_no, outcome, yes_ask, no_ask)
        print(
            f"{cutoff:<14}{int(r['n_bets']):>8,}{r['bet_rate']:>9.1%}"
            f"{r['win_rate']:>9.1%}{r['pnl']:>+12.2f}{r['roi']:>+9.2%}"
        )


def main() -> int:
    df = load_training_frame()
    if len(df) == 0:
        print("FAIL: fct_btc_15min_training returned 0 rows.", file=sys.stderr)
        return 1

    implied = df[BENCHMARK_COL].astype(float).to_numpy()
    mid = df["kalshi_mid_price"].astype(float).to_numpy()
    spread = df["kalshi_spread"].astype(float).to_numpy()
    y = df[TARGET_COL].astype(int).to_numpy()

    have = ~np.isnan(implied) & ~np.isnan(mid) & ~np.isnan(spread)
    implied, mid, spread, outcome = implied[have], mid[have], spread[have], y[have].astype(np.intp)

    yes_ask = np.clip(mid + spread / 2.0, 0.0, 1.0)
    yes_bid = np.clip(mid - spread / 2.0, 0.0, 1.0)
    no_ask = 1.0 - yes_bid

    print(f"Universe: {len(implied):,} settled windows with a decision-minute price")
    print(f"Overall up-rate: {outcome.mean():.3f}   mean implied: {implied.mean():.3f}")
    print(f"Median spread: {np.median(spread) * 100:.1f}c (the cost any edge must clear)\n")

    print("Part 1 — calibration by implied-probability bin (price vs reality)")
    _print_calibration(outcome, implied)

    # --- Part 2: is any tail tradeable after costs? -------------------------------
    # Favorite = the side priced over 0.5; bet it at its own ask. Deeper favorites
    # (higher fav_prob) are where the bias, if any, is largest.
    fav_is_yes = implied >= 0.5
    fav_prob = np.where(fav_is_yes, implied, 1.0 - implied)
    _print_strategy_sweep(
        "Part 2a — BET THE FAVORITE (fade the longshot), net of spread + fee",
        FAVORITE_THRESHOLDS, fav_prob, fav_is_yes, outcome, yes_ask, no_ask, favorite=True,
    )

    # Longshot = the side priced under 0.5; betting it is the control that SHOULD
    # lose if the classic bias holds (longshots are overpriced).
    dog_is_yes = implied < 0.5
    dog_prob = np.where(dog_is_yes, implied, 1.0 - implied)
    _print_strategy_sweep(
        "Part 2b — BET THE LONGSHOT (control: should lose if the bias is real)",
        LONGSHOT_THRESHOLDS, dog_prob, dog_is_yes, outcome, yes_ask, no_ask, favorite=False,
    )

    print("\nRead: if 'bet the favorite' turns net-positive at some cutoff AND the")
    print("calibration tail shows a matching significant gap, the favorite-longshot")
    print("bias is real and tradeable here. If both stay negative/flat, the market")
    print("prices the tails fairly too — another efficient-market null.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
