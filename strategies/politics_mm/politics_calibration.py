"""Is the documented POLITICS mispricing tradeable on Kalshi, net of costs?

The prediction-market literature (arXiv 2602.19520, 353M trades) finds POLITICS is by far the
WORST-calibrated Kalshi domain — ECE ~0.117 with prices COMPRESSED toward 50% (favorites
underpriced, longshots overpriced) — vs crypto/sports which are near-perfectly calibrated
(ECE ~0.007, matching our own 15-min BTC null). Politics is the one place the documented
mispricing (~12%) might EXCEED the spread + fee, instead of being trapped inside it like the
crypto favorite-longshot bias was (`strategies/btc_direction/favorite_longshot.py`).
This tests that on OUR
own data, market-internal (a decision-time price + the realized outcome — NO new ingestion),
reusing the exact favorite-longshot pattern:

  1. CALIBRATION — bin decision-time price vs realized outcome (reliability table, ECE, Brier,
     log loss). Compression shows as favorites (high price) winning MORE than priced and
     longshots (low price) winning LESS.
  2. COMPRESSION SLOPE — a logistic fit outcome ~ logit(price). slope > 1 = underconfident
     (compressed toward 50%, favorites underpriced) = the literature's claim; ~1 = calibrated.
  3. TRADEABILITY — buy the FAVORITE side (price > cutoff), HELD to resolution, under THREE entry
     regimes: TAKER@ask (pay the spread + taker fee — the naive test), MID (compression edge
     alone), and MAKER@bid (rest a bid, capture the full spread — the maker UPPER BOUND, since the
     taker lost BECAUSE it paid the spread the maker instead earns). CIs are EVENT-BLOCK
     bootstrapped (resample the RACE, not the candidate-market — within-race YES-markets sum to ~1
     and are NOT independent). MAKER@bid is gross of news-toxicity + months of inventory.

Decision-time price = the daily candle close at ~`--horizon-days` before the market's close
(genuine uncertainty; the closing price is near-deterministic and useless for calibration).

Usage:
    uv run python -m strategies.politics_mm.politics_calibration
    uv run python -m strategies.politics_mm.politics_calibration \
        --days 400 --horizon-days 7 --max-markets 3000
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from dotenv import load_dotenv
from sklearn.linear_model import LogisticRegression

from core.backtest.metrics import reliability_table, score
from ingestion.kalshi import KalshiClient, kalshi_taker_fee

POLITICAL_CATEGORIES = ("Politics", "Elections", "World", "Economics")
FAVORITE_CUTOFFS = (0.50, 0.60, 0.70, 0.80, 0.90)
N_BOOT = 3000
N_BOOT_SLOPE = 500
EPS = 1e-4
FloatArr = npt.NDArray[np.float64]


@dataclass
class Obs:
    """One resolved market at the decision horizon: its price, the realized outcome, the touch
    it could be traded at, and its RACE (event) for block resampling."""

    event: str
    price: float      # yes mid at the horizon
    outcome: int      # 1 if resolved YES
    yes_ask: float
    yes_bid: float


def political_series(client: KalshiClient, categories: tuple[str, ...], cap: int) -> list[str]:
    """Every series in the political categories (Politics/Elections/World/Economics)."""
    series = client.list_series()
    out = [s["ticker"] for s in series if s.get("category") in categories and s.get("ticker")]
    return out[:cap]


def _ts(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def decision_obs(
    client: KalshiClient, series: str, m: dict[str, Any], horizon_days: float
) -> Obs | None:
    """The daily-candle price ~horizon_days before close + the realized outcome. None if the
    market lacks a two-sided quote at the horizon or a yes/no result."""
    res = m.get("result")
    if res not in ("yes", "no") or not m.get("open_time") or not m.get("close_time"):
        return None
    ot, ct = _ts(m["open_time"]), _ts(m["close_time"])
    if ct - ot < horizon_days * 86400:  # market shorter than the horizon — no pre-resolution read
        return None
    target = ct - int(horizon_days * 86400)
    candles = client.get_market_candlesticks(series, m["ticker"], ot, ct, period_interval=1440)
    # the latest daily candle ending at/before `target` with a two-sided book
    best: Obs | None = None
    best_ts = -1
    for c in candles:
        cts = int(c.get("end_period_ts", 0))
        if cts > target or cts <= best_ts:
            continue
        yb = (c.get("yes_bid") or {}).get("close_dollars")
        ya = (c.get("yes_ask") or {}).get("close_dollars")
        if yb is None or ya is None:
            continue
        yb_f, ya_f = float(yb), float(ya)
        if 0.0 < yb_f <= ya_f < 1.0:
            best = Obs(m.get("event_ticker", m["ticker"]), (yb_f + ya_f) / 2.0,
                       1 if res == "yes" else 0, ya_f, yb_f)
            best_ts = cts
    return best


def collect(client: KalshiClient, days: int, horizon_days: float,
            max_series: int, max_markets: int) -> list[Obs]:
    now = dt.datetime.now(dt.UTC)
    start = now - dt.timedelta(days=days)
    series = political_series(client, POLITICAL_CATEGORIES, max_series)
    print(f"political series: {len(series)}  (categories {POLITICAL_CATEGORIES})")
    obs: list[Obs] = []
    checked = 0
    for i, st in enumerate(series):
        if len(obs) >= max_markets:
            break
        try:
            mkts = client.list_markets(st, status="settled",
                                       min_close_ts=int(start.timestamp()),
                                       max_close_ts=int(now.timestamp()))
        except Exception:  # noqa: BLE001 — a dead/renamed series shouldn't kill the sweep
            continue
        for m in mkts:
            checked += 1
            try:
                o = decision_obs(client, st, m, horizon_days)
            except Exception:  # noqa: BLE001 — a transient candlestick failure skips one market,
                continue       # not the whole (10-min) collection
            if o is not None:
                obs.append(o)
        if (i + 1) % 200 == 0:
            print(f"  ...{i + 1}/{len(series)} series, {len(obs)} priced obs so far")
    print(f"collected {len(obs)} priced resolved markets from {checked} settled markets")
    return obs


def _event_block_ci(
    pnl: FloatArr, events: list[str], rng: np.random.Generator
) -> tuple[float, float, float]:
    """Mean per-contract PnL + 95% CI resampling EVENTS (races) with replacement — the honest
    unit, since candidate-markets within a race are not independent."""
    by_ev: dict[str, list[float]] = defaultdict(list)
    for p, e in zip(pnl, events, strict=True):
        by_ev[e].append(p)
    ev_keys = list(by_ev.keys())
    ev_arrays = [np.array(by_ev[e]) for e in ev_keys]
    n = len(ev_keys)
    if n == 0:
        return float("nan"), float("nan"), float("nan")
    point = float(pnl.mean())
    boots = np.empty(N_BOOT)
    for b in range(N_BOOT):
        pick = rng.integers(0, n, n)
        boots[b] = np.concatenate([ev_arrays[i] for i in pick]).mean()
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return point, float(lo), float(hi)


def compression_slope(price: FloatArr, outcome: npt.NDArray[np.int_],
                      events: list[str], rng: np.random.Generator) -> tuple[float, float, float]:
    """Logistic slope of outcome on logit(price). >1 = compressed toward 50% (favorites
    underpriced); ~1 = calibrated; <1 = overconfident. Event-block bootstrap CI."""
    logit = np.log(np.clip(price, EPS, 1 - EPS) / (1 - np.clip(price, EPS, 1 - EPS)))

    def fit(x: FloatArr, y: npt.NDArray[np.int_]) -> float:
        if len(np.unique(y)) < 2:
            return float("nan")
        lr = LogisticRegression(C=1e6, solver="lbfgs")
        lr.fit(x.reshape(-1, 1), y)
        return float(lr.coef_[0][0])

    point = fit(logit, outcome)
    idx_by_ev: dict[str, list[int]] = defaultdict(list)
    for i, e in enumerate(events):
        idx_by_ev[e].append(i)
    ev_keys = list(idx_by_ev.keys())
    n = len(ev_keys)
    boots = []
    for _ in range(N_BOOT_SLOPE):
        pick = rng.integers(0, n, n)
        idx = np.concatenate([np.array(idx_by_ev[ev_keys[i]]) for i in pick])
        s = fit(logit[idx], outcome[idx])
        if not np.isnan(s):
            boots.append(s)
    lo, hi = (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))) if boots \
        else (float("nan"), float("nan"))
    return point, lo, hi


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi politics calibration + tradeability probe")
    ap.add_argument("--days", type=int, default=400, help="settled-market close window (days back)")
    ap.add_argument("--horizon-days", type=float, default=7.0, help="decision point before close")
    ap.add_argument("--max-series", type=int, default=5000)
    ap.add_argument("--max-markets", type=int, default=3000)
    ap.add_argument("--maker-fee-frac", type=float, default=0.0,
                    help="maker fee as a fraction of the taker fee (0 = maker-free)")
    args = ap.parse_args()

    load_dotenv()
    client = KalshiClient(pace_seconds=0.15)
    try:
        obs = collect(client, args.days, args.horizon_days, args.max_series, args.max_markets)
    finally:
        client.close()
    if len(obs) < 100:
        print(f"\nonly {len(obs)} obs — too few for a calibration read (politics resolves slowly; "
              "widen --days or the settled-listing horizon may be the limit).")
        return 0

    price = np.array([o.price for o in obs])
    outcome = np.array([o.outcome for o in obs], dtype=int)
    yes_ask = np.array([o.yes_ask for o in obs])
    yes_bid = np.array([o.yes_bid for o in obs])
    events = [o.event for o in obs]
    rng = np.random.default_rng(7)

    print("\n" + "=" * 84)
    print(f"POLITICS CALIBRATION — {len(obs)} resolved markets / {len(set(events))} races, "
          f"price @ {args.horizon_days:.0f}d before close")
    print("=" * 84)
    s = score(outcome, price)
    print(f"base rate (YES) {outcome.mean():.3f}   log_loss {s['log_loss']:.3f}   "
          f"brier {s['brier']:.3f}   ECE {s['ece']:.3f}   (crypto benchmark ECE ~0.005-0.02)")
    slope, slo, shi = compression_slope(price, outcome, events, rng)
    read = ("COMPRESSED toward 50% (favorites underpriced — lit's claim)" if slo > 1
            else "OVERCONFIDENT (favorites overpriced)" if shi < 1 else "~calibrated")
    print(f"compression slope {slope:.2f} [{slo:.2f},{shi:.2f}] (event-block) -> {read}")

    print("\nCALIBRATION CURVE (obs win-rate vs priced; obs>pred at high price + obs<pred at low = "
          "compression):")
    tbl = reliability_table(outcome, price)
    print(tbl[tbl["n"] >= 5].to_string(index=False,
          float_format=lambda x: f"{x:.3f}"))

    # Three entry regimes for the favorite side, HELD to resolution:
    #   TAKER@ask = pay the ask + taker fee (the earlier sweep — LOSES).
    #   MID       = enter at the mid: the compression edge ALONE (no spread paid or captured).
    #   MAKER@bid = rest a bid, get filled at the bid = capture the FULL spread. The maker UPPER
    #     bound — assumes you get filled (fill-rate optimism, per lp_paper_pilot), and is GROSS of
    #     news-toxicity + the MONTHS of directional inventory a political hold ties up.
    # NO-favorite (price<0.5) mirrors the YES book: no_ask=1-yes_bid, no_bid=1-yes_ask.
    fav_is_yes = price >= 0.5
    fav_win = np.where(fav_is_yes, outcome, 1 - outcome).astype(float)
    fav_prob = np.where(fav_is_yes, price, 1.0 - price)

    def regime_pnl(entry: FloatArr, maker: bool) -> FloatArr:
        frac = args.maker_fee_frac if maker else 1.0
        fee = np.array([kalshi_taker_fee(e) for e in entry]) * frac
        return fav_win - entry - fee

    regimes = {
        "TAKER@ask": regime_pnl(np.where(fav_is_yes, yes_ask, 1.0 - yes_bid), maker=False),
        "MID": regime_pnl(np.where(fav_is_yes, price, 1.0 - price), maker=True),
        "MAKER@bid": regime_pnl(np.where(fav_is_yes, yes_bid, 1.0 - yes_ask), maker=True),
    }
    print("\nTRADEABILITY — buy the favorite (price > cutoff), HOLD to resolution. mean pnl/ct +")
    print("event-block 95% CI. TAKER pays spread; MID neither; MAKER captures it (upper bound):")
    print(f"  {'cutoff':>6}{'bets':>6}   " + "".join(f"{r:>25}" for r in regimes))
    for thr in FAVORITE_CUTOFFS:
        q = fav_prob > thr
        if int(q.sum()) < 8:
            print(f"  {thr:>6.2f}{int(q.sum()):>6}   (too few)")
            continue
        ev = [e for e, k in zip(events, q, strict=True) if k]
        cells = "".join(
            f"{p:+.4f}[{lo:+.3f},{hi:+.3f}]".rjust(25)
            for p, lo, hi in (_event_block_ci(pnl[q], ev, rng) for pnl in regimes.values())
        )
        print(f"  {thr:>6.2f}{int(q.sum()):>6}   {cells}")

    print("\nRead: MID isolates the compression edge (net of fee, no spread). MAKER@bid adds full")
    print("spread capture = the MAKER UPPER BOUND. If MAKER@bid's CI doesn't clear 0, it's")
    print("closed for a maker too. If it DOES, compression is a gross tailwind — but still")
    print("gross of news-toxicity + MONTHS of directional inventory (the real killers).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
