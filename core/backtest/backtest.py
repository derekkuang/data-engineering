"""Cost-aware walk-forward PnL backtest: is the model's edge actually money?

The baseline showed the model barely edges the market on log loss once the feature
leak is removed (~-0.006). This asks the only question that ultimately matters:
after paying the REAL Kalshi spread and fees, is there any profit left? It turns
the same out-of-fold probabilities into simulated bets and tallies PnL, against
two reference strategies — "follow the market" and "never bet".

Kalshi economics (prices in dollars; 1 contract settles to $1):
  * buy YES at yes_ask = mid + spread/2   -> pays $1 if BTC ends up
  * buy NO  at no_ask  = 1 - yes_bid      -> pays $1 if BTC ends down
  * trade fee per contract ~= ceil_to_cent(0.07 * price * (1-price)); settlement is
    free, and a binary held to expiry has no exit trade, so cost = price + fee.

The bet rule overcomes the spread by construction: bet a side only when the model's
probability clears that side's ASK price by `threshold`. So the cost is baked into
the decision to trade, not bolted on afterward. We sweep the threshold to see if
ANY level is profitable.

The real Kalshi quote (kalshi_mid_price, kalshi_spread) is already carried to the
decision minute in fct_btc_15min_training, so no extra data is needed.

Usage:
    uv run python -m core.backtest.backtest
"""

import sys

import numpy as np
import numpy.typing as npt

from core.backtest.data import TARGET_COL, feature_matrix, load_training_frame
from core.backtest.model import logistic_pipeline, walk_forward_oof

N_SPLITS = 8
FEE_RATE = 0.07  # Kalshi trade-fee coefficient in fee = 0.07 * price * (1 - price)
THRESHOLDS = (0.00, 0.01, 0.02, 0.05, 0.10)
# Mirror train_baseline: add "log_return_1m" to test how much of the PnL is the
# within-window lead-lag head start (BTC moves before the Kalshi quote reprices).
DROP_FEATURES: list[str] = []

FloatArr = npt.NDArray[np.float64]
IntArr = npt.NDArray[np.intp]


def _fee(price: FloatArr) -> FloatArr:
    """Kalshi per-contract trade fee: round UP to the next cent of 0.07*P*(1-P)."""
    return np.ceil(FEE_RATE * price * (1.0 - price) * 100.0) / 100.0


def _per_window_pnl(
    bet_yes: npt.NDArray[np.bool_],
    bet_no: npt.NDArray[np.bool_],
    outcome: IntArr,
    yes_ask: FloatArr,
    no_ask: FloatArr,
) -> tuple[FloatArr, FloatArr]:
    """Per-window (pnl, stake) for a flat one-contract bet at the given asks, net of
    the Kalshi fee. The single shared money-math primitive: `_summarise` aggregates
    it, and the bootstrap scripts (decision_minute_profit, live_cluster_verdict)
    consume the raw arrays so day-resampling stays possible."""
    yes_pnl = np.where(outcome == 1, 1.0, 0.0) - yes_ask - _fee(yes_ask)
    no_pnl = np.where(outcome == 0, 1.0, 0.0) - no_ask - _fee(no_ask)
    pnl = np.where(bet_yes, yes_pnl, np.where(bet_no, no_pnl, 0.0))
    staked = np.where(
        bet_yes, yes_ask + _fee(yes_ask), np.where(bet_no, no_ask + _fee(no_ask), 0.0)
    )
    return pnl, staked


def _summarise(
    bet_yes: npt.NDArray[np.bool_],
    bet_no: npt.NDArray[np.bool_],
    outcome: IntArr,
    yes_ask: FloatArr,
    no_ask: FloatArr,
) -> dict[str, float]:
    """Flat one-contract stake per qualifying window; tally PnL net of fees."""
    pnl, staked = _per_window_pnl(bet_yes, bet_no, outcome, yes_ask, no_ask)
    made_bet = bet_yes | bet_no
    wins = (bet_yes & (outcome == 1)) | (bet_no & (outcome == 0))

    n_bets = int(made_bet.sum())
    total_staked = float(staked.sum())
    total_pnl = float(pnl.sum())
    return {
        "n_bets": float(n_bets),
        "bet_rate": float(made_bet.mean()),
        "win_rate": float(wins.sum() / n_bets) if n_bets else float("nan"),
        "pnl": total_pnl,
        "roi": (total_pnl / total_staked) if total_staked else float("nan"),
    }


def _print_row(label: str, r: dict[str, float]) -> None:
    print(
        f"{label:<16}{int(r['n_bets']):>8,}{r['bet_rate']:>9.1%}"
        f"{r['win_rate']:>9.1%}{r['pnl']:>+12.2f}{r['roi']:>+9.2%}"
    )


def _effective_quote(
    mid: FloatArr, spread: FloatArr, mult: float, slippage: float
) -> tuple[FloatArr, FloatArr, FloatArr]:
    """Widen the recorded quote: half-spread scaled by `mult` plus a flat `slippage`
    (both in dollars). Returns the (yes_ask, yes_bid, no_ask) you'd really pay."""
    half = spread / 2.0 * mult + slippage
    yes_ask = np.clip(mid + half, 0.0, 1.0)
    yes_bid = np.clip(mid - half, 0.0, 1.0)
    return yes_ask, yes_bid, 1.0 - yes_bid


def _breakeven_cost(
    prob: FloatArr, outcome: IntArr, mid: FloatArr, spread: FloatArr, threshold: float = 0.0
) -> float:
    """Smallest extra per-side cost (dollars) added on top of the recorded spread
    that drives the model's PnL to <= 0. NaN if it stays profitable past +10c."""
    for step in range(0, 101):
        extra = step / 1000.0  # 0.1-cent increments, 0 .. 10.0c inclusive
        yes_ask, yes_bid, no_ask = _effective_quote(mid, spread, 1.0, extra)
        summary = _summarise(
            (prob - yes_ask) > threshold, (yes_bid - prob) > threshold, outcome, yes_ask, no_ask
        )
        if summary["pnl"] <= 0.0:
            return extra
    return float("nan")


def main() -> int:
    df = load_training_frame()
    n = len(df)
    if n == 0:
        print("FAIL: fct_btc_15min_training returned 0 rows.", file=sys.stderr)
        return 1

    # Same features and walk-forward as the baseline → same out-of-fold probs.
    x, feats = feature_matrix(df, drop=tuple(DROP_FEATURES))
    y = df[TARGET_COL].astype(int).to_numpy()
    oof = walk_forward_oof(x, y, build_estimator=logistic_pipeline, n_splits=N_SPLITS)

    # Reconstruct the real quote at the decision minute from mid + spread.
    mid = df["kalshi_mid_price"].astype(float).to_numpy()
    spread = df["kalshi_spread"].astype(float).to_numpy()
    yes_ask = np.clip(mid + spread / 2.0, 0.0, 1.0)
    yes_bid = np.clip(mid - spread / 2.0, 0.0, 1.0)
    no_ask = 1.0 - yes_bid

    # Universe: out-of-fold rows that also have a live quote.
    live = ~np.isnan(oof) & ~np.isnan(mid) & ~np.isnan(spread)
    prob, outcome = oof[live], y[live]
    ask, bid, n_ask = yes_ask[live], yes_bid[live], no_ask[live]

    print(f"Backtest universe: {int(live.sum()):,} out-of-fold windows with a live quote")
    print(f"Features: {len(feats)}   median spread: {np.median(spread[live]) * 100:.1f}c   ")
    print()
    mkt = mid[live]
    print(f"{'strategy':<16}{'bets':>8}{'bet%':>9}{'win%':>9}{'PnL($)':>12}{'ROI':>9}")
    print("-" * 63)

    # Model strategy across decision thresholds.
    for thr in THRESHOLDS:
        _print_row(
            f"model t={thr:.2f}",
            _summarise((prob - ask) > thr, (bid - prob) > thr, outcome, ask, n_ask),
        )

    print("-" * 63)
    # No-skill controls. A strategy with no real edge should LOSE money (it pays
    # the spread + fee on every bet). If any control also profits, the model's PnL
    # is a structural/cost artifact, not signal — the same scrutiny that caught the
    # kalshi_mid_price leak.
    m_yes, m_no = (prob - ask) > 0.0, (bid - prob) > 0.0
    rand = np.random.default_rng(0).random(len(prob))
    controls = [
        ("follow-market", mkt >= 0.5, mkt < 0.5),  # always bet the favourite
        ("fade-favorite", mkt < 0.5, mkt >= 0.5),  # always bet the underdog
        ("anti-model", m_no, m_yes),               # bet opposite the model
        ("random", rand >= 0.5, rand < 0.5),       # coin-flip side
    ]
    for label, bet_yes, bet_no in controls:
        _print_row(label, _summarise(bet_yes, bet_no, outcome, ask, n_ask))

    print("\nControls check: a no-skill strategy should lose money (cost drag).")
    print("If 'fade-favorite' ~ matches the model, the 'edge' is fading the noisy")
    print("first-minute quote, not ML — investigate before trusting it.")

    # --- Cost sensitivity: how much must the spread widen to kill the edge? ---
    base_spread = spread[live]
    print()
    print("Cost sensitivity (model t=0.00) — widen spread / add slippage:")
    print(f"{'assumed cost':<22}{'bets':>8}{'PnL($)':>12}{'ROI':>9}")
    print("-" * 51)
    for label, mult, slip in (
        ("recorded (1x)", 1.0, 0.000),
        ("2x spread", 2.0, 0.000),
        ("3x spread", 3.0, 0.000),
        ("1x + 1c slippage", 1.0, 0.010),
        ("1x + 2c slippage", 1.0, 0.020),
    ):
        ya, yb, na = _effective_quote(mkt, base_spread, mult, slip)
        r = _summarise((prob - ya) > 0.0, (yb - prob) > 0.0, outcome, ya, na)
        print(f"{label:<22}{int(r['n_bets']):>8,}{r['pnl']:>+12.2f}{r['roi']:>+9.2%}")

    breakeven = _breakeven_cost(prob, outcome, mkt, base_spread)
    median_spread = float(np.median(base_spread)) * 100.0
    print()
    if np.isnan(breakeven):
        print("Breakeven: still profitable at +10c extra cost — very robust (suspicious).")
    else:
        print(
            f"Breakeven: edge dies at ~{breakeven * 100:.1f}c extra per-side cost on top of "
            f"the recorded ~{median_spread:.1f}c spread."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
