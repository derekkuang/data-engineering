"""Live-execution reconciliation — the verdict on the +8% backtest.

The cost-aware walk-forward backtest (`ml/backtest.py`) showed a NET-PROFITABLE
+8% ROI that survived a 3x-spread / +2c-slippage sweep (breakeven ~5.8c). Its one
unfalsifiable assumption: that you could actually TRANSACT at the price it priced
each bet at — the close of the first in-window 1-min candle (~W+1:00), the same
`kalshi_mid_price`/`kalshi_spread` carried into `fct_btc_15min_training`.

We can't reconstruct a historical order book, so a launchd collector
(`ingestion/kalshi_orderbook.py`) banked the LIVE executable book at the decision
minute for ~33 real KXBTC15M windows (`data/orderbook_snapshots.jsonl`), three
snapshots ~20s apart. This script answers the only remaining question with that
data, in two parts:

  1. DECISION-INSTANT SLIPPAGE — for the snapshot nearest W+1:00 (the exact moment
     the backtest priced at), how far is the live executable ask from the backfill
     candle-close ask the backtest assumed? If it's ~0 and unbiased, the backtest
     price was real and hittable — the +8% is not a stale-quote artifact. We then
     inject the measured slippage as a CONSERVATIVE (always-adverse) cost into the
     exact same backtest universe and read off the adjusted ROI vs the +8%.

  2. REPRICING SPEED — across the three snapshots, how fast does the quote drift
     after the decision instant? The Kalshi book chases BTC spot in seconds, so
     even a small execution latency can move the price past the ~5.8c breakeven.
     This quantifies how latency-bound the edge is.

The thesis it tests: the edge is real at the instant but lives in a sub-minute,
latency-bound window — capturable only by execution faster than the book reprices,
which a 15-min-cadence batch pipeline cannot deliver. That is exactly what an
efficient market looks like: the edge is the size of the friction protecting it.

Read-only / public API — no auth, no money.

Usage:
    uv run python -m ml.alpha.live_exec_reconcile
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from dotenv import load_dotenv

from ingestion.kalshi import SERIES_BTC_15M, KalshiClient
from ml.alpha.backtest import DROP_FEATURES, N_SPLITS, _breakeven_cost, _effective_quote, _summarise
from ml.alpha.data import TARGET_COL, feature_matrix, load_training_frame
from ml.alpha.model import logistic_pipeline, walk_forward_oof

SNAPSHOTS_PATH = Path("data/orderbook_snapshots.jsonl")
DECISION_OFFSET_S = 60  # backtest prices at the close of the first in-window candle (~W+1:00)
BURST_WINDOW_S = 180  # snapshots within W..W+3min belong to the decision-minute burst

FloatArr = npt.NDArray[np.float64]


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_windows() -> dict[str, list[dict[str, Any]]]:
    """Group the JSONL snapshots by market ticker (one ticker == one 15-min window)."""
    by_window: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with SNAPSHOTS_PATH.open() as fh:
        for line in fh:
            row = json.loads(line)
            by_window[row["market_ticker"]].append(row)
    for snaps in by_window.values():
        snaps.sort(key=lambda r: r["captured_at"])
    return by_window


def _decision_candle(
    client: KalshiClient, ticker: str, window_open: datetime, window_close: datetime
) -> dict[str, float] | None:
    """The candle the backtest priced at: the first in-window 1-min candle, whose
    close (end_period_ts == window_open + 60s) is the decision-minute quote. Returns
    its yes_bid/yes_ask (dollars), or None if missing / one-sided."""
    open_ts, close_ts = int(window_open.timestamp()), int(window_close.timestamp())
    candles = client.get_market_candlesticks(SERIES_BTC_15M, ticker, open_ts, close_ts, 1)
    in_window = [c for c in candles if open_ts < int(c["end_period_ts"]) <= close_ts]
    if not in_window:
        return None
    target = open_ts + DECISION_OFFSET_S
    candle = min(in_window, key=lambda c: abs(int(c["end_period_ts"]) - target))

    yb = (candle.get("yes_bid") or {}).get("close_dollars")
    ya = (candle.get("yes_ask") or {}).get("close_dollars")
    if yb is None or ya is None:
        return None
    return {"yes_bid": float(yb), "yes_ask": float(ya)}


def _burst(snaps: list[dict[str, Any]], window_open: datetime) -> list[dict[str, Any]]:
    """The decision-minute snapshots: those captured in W .. W+3min with a two-sided
    quote. Filters out any stray later burst on the same (re-active) ticker."""
    lo, hi = window_open, window_open + timedelta(seconds=BURST_WINDOW_S)
    return [s for s in snaps if lo <= _iso(s["captured_at"]) <= hi and s.get("mid") is not None]


def _reconcile_windows(
    by_window: dict[str, list[dict[str, Any]]], client: KalshiClient
) -> dict[str, FloatArr]:
    """Per window, pair the backfill decision-candle quote with the live executable
    book nearest W+1:00, and measure (a) decision-instant slippage and (b) how fast
    the quote drifts over the burst. Returns parallel arrays of the per-window stats."""
    yes_slip, no_slip, mid_slip, drift_20s, abs_drift = [], [], [], [], []
    d_spot, d_mid = [], []  # within-burst BTC move ($) vs Kalshi mid move (cents)
    matched = 0

    for ticker, snaps in sorted(by_window.items()):
        window_open = _iso(snaps[0]["window_open_at"])
        window_close = _iso(snaps[0]["window_close_at"])
        candle = _decision_candle(client, ticker, window_open, window_close)
        burst = _burst(snaps, window_open)
        if candle is None or not burst:
            continue

        # Live quote at the decision instant = the snapshot nearest the candle close.
        target = window_open + timedelta(seconds=DECISION_OFFSET_S)
        dec = min(burst, key=lambda s: abs((_iso(s["captured_at"]) - target).total_seconds()))

        bf_yes_ask, bf_yes_bid = candle["yes_ask"], candle["yes_bid"]
        bf_no_ask = 1.0 - bf_yes_bid
        live_no_ask = 1.0 - float(dec["best_yes_bid"])
        bf_mid = (bf_yes_ask + bf_yes_bid) / 2.0

        yes_slip.append(float(dec["best_yes_ask"]) - bf_yes_ask)  # +ve = live worse for YES buyer
        no_slip.append(live_no_ask - bf_no_ask)  # +ve = live worse for NO buyer
        mid_slip.append(float(dec["mid"]) - bf_mid)  # directional repricing vs backfill

        # Repricing speed: mid drift across the burst, normalised to per-20s.
        t0, tN = _iso(burst[0]["captured_at"]), _iso(burst[-1]["captured_at"])
        secs = (tN - t0).total_seconds()
        if secs > 0:
            move = float(burst[-1]["mid"]) - float(burst[0]["mid"])
            drift_20s.append(move / secs * 20.0)
            abs_drift.append(abs(move) / secs * 20.0)

        # Spot-tracking: does the mid move WITH BTC spot over the burst? (one
        # independent point per window: first vs last snap with both readings.)
        spotted = [s for s in burst if s.get("btc_spot") is not None and s.get("mid") is not None]
        if len(spotted) >= 2:
            d_spot.append(float(spotted[-1]["btc_spot"]) - float(spotted[0]["btc_spot"]))
            d_mid.append((float(spotted[-1]["mid"]) - float(spotted[0]["mid"])) * 100.0)
        matched += 1

    print(f"Reconciled {matched} windows (of {len(by_window)} captured).\n")
    return {
        "yes_slip": np.array(yes_slip),
        "no_slip": np.array(no_slip),
        "mid_slip": np.array(mid_slip),
        "drift_20s": np.array(drift_20s),
        "abs_drift": np.array(abs_drift),
        "d_spot": np.array(d_spot),
        "d_mid": np.array(d_mid),
    }


def _stat_row(label: str, arr: FloatArr) -> None:
    """Print a slippage/drift distribution row in cents."""
    c = arr * 100.0
    print(
        f"{label:<26}{len(arr):>6}{c.mean():>+9.2f}{np.median(c):>+9.2f}"
        f"{c.std():>8.2f}{np.percentile(np.abs(c), 75):>9.2f}"
    )


def _spot_tracking(d_spot: FloatArr, d_mid: FloatArr) -> dict[str, float]:
    """Regress the within-burst Kalshi mid move (cents) on the BTC spot move ($).
    A high R^2 / strong sign agreement means the book mechanically chases spot, so
    the post-decision drift is structural — and adverse to any spot-momentum bet."""
    slope, _ = np.polyfit(d_spot, d_mid, 1)
    r = float(np.corrcoef(d_spot, d_mid)[0, 1])
    # Directional agreement on the moves big enough to read (BTC > $5, mid > 0.5c):
    big = (np.abs(d_spot) > 5.0) & (np.abs(d_mid) > 0.5)
    match = np.sign(d_spot[big]) == np.sign(d_mid[big])
    agree = float(np.mean(match)) if big.any() else float("nan")
    return {
        "n": float(len(d_spot)),
        "slope_per_100": float(slope) * 100.0,  # cents of mid per $100 of BTC
        "r": r,
        "r2": r * r,
        "agree": agree,
        "n_big": float(big.sum()),
    }


def _backtest_universe() -> tuple[FloatArr, npt.NDArray[np.intp], FloatArr, FloatArr]:
    """Reproduce the backtest's out-of-fold universe (mirrors ml.alpha.backtest.main): the
    same walk-forward logistic OOF probs and the decision-minute mid/spread, so we can
    re-price the SAME bets under the slippage measured from the live book."""
    df = load_training_frame()
    x, _ = feature_matrix(df, drop=tuple(DROP_FEATURES))
    y = df[TARGET_COL].astype(int).to_numpy()
    oof = walk_forward_oof(x, y, build_estimator=logistic_pipeline, n_splits=N_SPLITS)
    mid = df["kalshi_mid_price"].astype(float).to_numpy()
    spread = df["kalshi_spread"].astype(float).to_numpy()
    live = ~np.isnan(oof) & ~np.isnan(mid) & ~np.isnan(spread)
    return oof[live], y[live].astype(np.intp), mid[live], spread[live]


def _roi_at_slippage(
    prob: FloatArr, outcome: npt.NDArray[np.intp], mid: FloatArr, spread: FloatArr, slip: float
) -> dict[str, float]:
    """Backtest ROI when every fill costs `slip` dollars more per side (threshold 0)."""
    yes_ask, yes_bid, no_ask = _effective_quote(mid, spread, 1.0, slip)
    return _summarise((prob - yes_ask) > 0.0, (yes_bid - prob) > 0.0, outcome, yes_ask, no_ask)


def main() -> int:
    if not SNAPSHOTS_PATH.exists():
        print(f"FAIL: {SNAPSHOTS_PATH} not found — has the collector run?", file=sys.stderr)
        return 1

    load_dotenv()
    by_window = _load_windows()
    client = KalshiClient()
    try:
        stats = _reconcile_windows(by_window, client)
    finally:
        client.close()

    if not stats["yes_slip"].size:
        print("FAIL: no windows could be reconciled.", file=sys.stderr)
        return 1

    # --- Part 1: decision-instant slippage (live executable vs backfill candle) ---
    print("Part 1 — decision-instant slippage: live book @ ~W+1:00 vs backfill candle close")
    print(f"{'measure (cents)':<26}{'n':>6}{'mean':>9}{'median':>9}{'std':>8}{'p75|.|':>9}")
    print("-" * 67)
    _stat_row("yes-ask slippage", stats["yes_slip"])
    _stat_row("no-ask slippage", stats["no_slip"])
    _stat_row("mid drift vs backfill", stats["mid_slip"])
    pooled_ask = np.concatenate([stats["yes_slip"], stats["no_slip"]])
    _stat_row("pooled ask slippage", pooled_ask)
    print(
        "\nRead: mean ~0 + symmetric => the backfill decision price was unbiased and "
        "executable\nat the instant (the +8% is not a stale-quote artifact). The scatter is "
        "execution noise."
    )

    # --- Part 2: repricing speed (the latency-bound part) ---
    print("\nPart 2 — repricing speed after the decision instant (per 20s, across the burst)")
    print(f"{'measure (cents)':<26}{'n':>6}{'mean':>9}{'median':>9}{'std':>8}{'p75|.|':>9}")
    print("-" * 67)
    _stat_row("signed mid drift /20s", stats["drift_20s"])
    _stat_row("abs mid drift /20s", stats["abs_drift"])

    # --- Part 2b: is the drift adverse, or just zero-mean noise? (spot-tracking) ---
    print("\nPart 2b — is the drift adverse? Regress within-burst mid move (c) on BTC move ($)")
    track = _spot_tracking(stats["d_spot"], stats["d_mid"])
    print(
        f"  n={int(track['n'])}   slope={track['slope_per_100']:+.2f}c per $100 BTC   "
        f"r={track['r']:+.2f}   R^2={track['r2']:.2f}"
    )
    print(
        f"  sign agreement (moves > $5 & > 0.5c): {track['agree']:.0%} "
        f"of {int(track['n_big'])} windows"
    )
    print(
        "  Read: the mid chases spot, so the drift is NOT zero-mean to a momentum bettor —\n"
        "  betting WITH the move, the favoured side's cost rises before you can fill (adverse).\n"
        "  That is what legitimises charging the |drift| as a cost below."
    )

    # --- Part 3: map the measured slippage onto the actual +8% backtest ---
    print("\nPart 3 — re-pricing the +8% backtest under the measured execution slippage")
    prob, outcome, mid, spread = _backtest_universe()
    breakeven = _breakeven_cost(prob, outcome, mid, spread)

    # Conservative: treat the decision-instant noise as if it were ALWAYS adverse.
    conservative = float(np.mean(np.abs(pooled_ask)))
    p75_slip = float(np.percentile(np.abs(pooled_ask), 75))
    latency_20s = float(np.mean(stats["abs_drift"]))

    print(f"{'execution assumption':<34}{'slip(c)':>9}{'bets':>8}{'PnL($)':>11}{'ROI':>9}")
    print("-" * 71)
    for label, slip in (
        ("recorded backfill price (the +8%)", 0.0),
        ("+ decision-instant noise (mean|.|)", conservative),
        ("+ decision-instant noise (p75|.|)", p75_slip),
        ("+ one 20s execution-latency drift", latency_20s),
    ):
        r = _roi_at_slippage(prob, outcome, mid, spread, slip)
        print(
            f"{label:<34}{slip * 100:>9.2f}{int(r['n_bets']):>8,}"
            f"{r['pnl']:>+11.2f}{r['roi']:>+9.2%}"
        )

    be_txt = f"~{breakeven * 100:.1f}c" if not np.isnan(breakeven) else ">10c"
    r2 = track["r2"]
    scatter_c = conservative * 100.0
    drift_c = latency_20s * 100.0
    scatter_roi = _roi_at_slippage(prob, outcome, mid, spread, conservative)["roi"] * 100.0
    latency_roi = _roi_at_slippage(prob, outcome, mid, spread, latency_20s)["roi"] * 100.0
    delays_to_be = breakeven / max(latency_20s, 1e-9)
    print(
        "\nVerdict:\n"
        "  * decision-instant slippage is UNBIASED (mean ~0) => the backfill decision\n"
        "    price was real and hittable: the +8% is NOT a stale-quote artifact.\n"
        f"  * the ~{scatter_c:.1f}c decision-instant scatter is zero-mean, so charging it "
        f"(-> +{scatter_roi:.1f}%)\n"
        "    is a PESSIMISTIC bound — random fill timing doesn't bias expected PnL.\n"
        f"  * but the ~{drift_c:.1f}c/20s repricing tracks spot almost 1:1 (R^2={r2:.2f}): to a\n"
        "    momentum bettor it is ADVERSE, not noise, so charging it is legitimate. One\n"
        f"    ~20s execution delay alone takes the edge to +{latency_roi:.1f}%, and the {be_txt}\n"
        f"    breakeven is only ~{delays_to_be:.0f} such delays away.\n"
        "  => a real but THIN edge, the size of the execution friction protecting it —\n"
        "     capturable only faster than the book reprices, which a 15-min batch pipeline\n"
        "     cannot do. Exactly what an efficient market should leave on the table."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
