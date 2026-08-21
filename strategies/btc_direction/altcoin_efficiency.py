"""Are the thinner Kalshi 15-min markets (ETH, HYPE) less efficiently priced than BTC?

The BTC lead-lag is a latency race we lose; thinner markets have less MM
competition, so they're the best shot at a STATIC, no-race mispricing — a
calibration skew or a favorite-longshot bias. This fetches settled KXETH15M /
KXHYPE15M windows (decision-minute implied prob + settlement) straight from the
public API and runs the same calibration + favorite-longshot tests we ran on BTC,
with BTC fetched alongside as the calibrated reference.

Market-internal only: it needs just the Kalshi price + outcome, so it works for
any asset without ingesting that asset's spot feed (that's only needed for the
heavier forecasting/lead-lag test). Reuses ingestion.kalshi_backfill._fetch_candles
(the batched candle fetch) and core.backtest.metrics / core.backtest.backtest.

Usage:
    uv run python -m strategies.btc_direction.altcoin_efficiency --days 45
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

import numpy as np
import numpy.typing as npt
from dotenv import load_dotenv

from core.backtest.backtest import _summarise
from core.backtest.metrics import reliability_table, score
from ingestion.kalshi import KalshiCandle, KalshiClient
from ingestion.kalshi_backfill import _fetch_candles

SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "HYPE": "KXHYPE15M"}
# Favorite DEPTH cutoffs: bet the favourite only when its price clears this level,
# so deeper cutoffs isolate the strong favourites where a longshot bias would show.
FAVORITE_THRESHOLDS = (0.50, 0.60, 0.70, 0.80, 0.90)

FloatArr = npt.NDArray[np.float64]
IntArr = npt.NDArray[np.intp]


def fetch_windows(client: KalshiClient, series: str, days: int) -> dict[str, FloatArr]:
    """One row per settled window: the decision-minute (first in-window ~W+1) implied
    prob + yes bid/ask, and the settlement outcome. Drops windows lacking a two-sided
    decision-minute quote or a yes/no result."""
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    start = now - dt.timedelta(days=days)
    markets = client.list_markets(
        series, status="settled",
        min_close_ts=int(start.timestamp()), max_close_ts=int(now.timestamp()),
    )
    candles = _fetch_candles(client, markets, series)

    by_win: dict[str, list[KalshiCandle]] = defaultdict(list)
    for c in candles:
        by_win[c.market_ticker].append(c)

    prob, outcome, yes_ask, yes_bid = [], [], [], []
    for cs in by_win.values():
        wo = cs[0].window_open_at
        usable = [
            c for c in cs
            if c.event_at > wo
            and c.implied_prob_close is not None
            and c.yes_bid_close is not None
            and c.yes_ask_close is not None
        ]
        if not usable:
            continue
        d = min(usable, key=lambda c: c.event_at)
        p, ya, yb = d.implied_prob_close, d.yes_ask_close, d.yes_bid_close
        if d.result not in ("yes", "no") or p is None or ya is None or yb is None:
            continue
        prob.append(float(p))
        outcome.append(1 if d.result == "yes" else 0)
        yes_ask.append(float(ya))
        yes_bid.append(float(yb))

    return {
        "prob": np.array(prob), "outcome": np.array(outcome),
        "yes_ask": np.array(yes_ask), "yes_bid": np.array(yes_bid),
    }


def _favorite_sweep(d: dict[str, FloatArr]) -> None:
    """Bet the favourite (the side priced > 0.5) at its ask, net of fee, across
    price cutoffs — the favorite-longshot test that needs no forecast."""
    prob = d["prob"]
    outcome = d["outcome"].astype(np.intp)
    yes_ask, yes_bid = d["yes_ask"], d["yes_bid"]
    no_ask = 1.0 - yes_bid
    fav_is_yes = prob >= 0.5
    fav_prob = np.where(fav_is_yes, prob, 1.0 - prob)

    print(f"    {'cutoff':<9}{'bets':>7}{'bet%':>8}{'win%':>8}{'PnL($)':>10}{'ROI':>9}")
    for thr in FAVORITE_THRESHOLDS:
        qualify = fav_prob > thr
        r = _summarise(qualify & fav_is_yes, qualify & ~fav_is_yes, outcome, yes_ask, no_ask)
        print(
            f"    >={thr:<6.2f}{int(r['n_bets']):>7,}{r['bet_rate']:>8.1%}"
            f"{r['win_rate']:>8.1%}{r['pnl']:>+10.2f}{r['roi']:>+9.2%}"
        )


def analyze(name: str, series: str, d: dict[str, FloatArr]) -> None:
    n = len(d["prob"])
    print(f"\n=== {name} ({series}) — {n} settled windows ===")
    if n < 30:
        print(f"  too few windows ({n}) for a calibration read — likely short history / thin book")
        if n == 0:
            return
    outcome = d["outcome"].astype(int)
    spread = (d["yes_ask"] - d["yes_bid"])
    s = score(outcome, d["prob"])
    print(
        f"  up-rate {outcome.mean():.3f}   median spread {np.median(spread) * 100:.1f}c   "
        f"mean implied {d['prob'].mean():.3f}"
    )
    print(
        f"  calibration: log_loss {s['log_loss']:.3f}   brier {s['brier']:.3f}   "
        f"acc {s['accuracy']:.1%}   ECE {s['ece']:.3f}"
    )
    # tails are where a favorite-longshot bias hides
    table = reliability_table(outcome, d["prob"])
    tail = table[(table["n"] >= 5)]
    if len(tail):
        worst = tail.iloc[(tail["pred_mean"] - tail["obs_freq"]).abs().argmax()]
        print(
            f"  worst-calibrated bin {worst['bin']}: price {worst['pred_mean']:.2f} vs "
            f"realized {worst['obs_freq']:.2f} (n={int(worst['n'])})"
        )
    _favorite_sweep(d)


def main() -> int:
    parser = argparse.ArgumentParser(description="Kalshi 15-min efficiency scan (ETH/HYPE vs BTC)")
    parser.add_argument("--days", type=int, default=45, help="settled history to fetch")
    args = parser.parse_args()

    load_dotenv()
    client = KalshiClient()
    print(f"Fetching ~{args.days}d of settled windows per series (BTC = calibrated reference)...")
    try:
        for name, series in SERIES.items():
            d = fetch_windows(client, series, args.days)
            analyze(name, series, d)
    finally:
        client.close()

    print(
        "\nRead: log_loss ~0.69 = no better than a coin flip (uninformative); ~0.66 = the\n"
        "informative BTC bar. ECE = miscalibration. Observed pattern: BTC (~1c spread) and\n"
        "ETH (~2c) are equally calibrated and uninformative-to-beat; the thinnest market\n"
        "(HYPE, ~9c spread) IS measurably less efficient (coin-flip log loss, miscalibrated\n"
        "tails) — but its spread widens in lockstep, so betting the favourite still loses\n"
        "~13%. Less competition => MORE mispricing AND MORE friction, and the friction wins.\n"
        "The deepest-favourite (>=0.90) +ROI is n<15 noise, not signal. No tradeable edge."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
