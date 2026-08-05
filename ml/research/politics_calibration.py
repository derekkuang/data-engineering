"""Is the documented POLITICS mispricing tradeable on Kalshi, net of costs?

The prediction-market literature (arXiv 2602.19520, 353M trades) finds POLITICS is by far the
WORST-calibrated Kalshi domain — ECE ~0.117 with prices COMPRESSED toward 50% (favorites
underpriced, longshots overpriced) — vs crypto/sports which are near-perfectly calibrated
(ECE ~0.007, matching our own 15-min BTC null). Politics is the one place the documented
mispricing (~12%) might EXCEED the spread + fee, instead of being trapped inside it like the
crypto favorite-longshot bias was (`ml/alpha/favorite_longshot.py`). This tests that on OUR
own data, market-internal (a decision-time price + the realized outcome — NO new ingestion),
reusing the exact favorite-longshot pattern:

  1. CALIBRATION — bin decision-time price vs realized outcome (reliability table, ECE, Brier,
     log loss). Compression shows as favorites (high price) winning MORE than priced and
     longshots (low price) winning LESS.
  2. COMPRESSION SLOPE — a logistic fit outcome ~ logit(price). slope > 1 = underconfident
     (compressed toward 50%, favorites underpriced) = the literature's claim; ~1 = calibrated.
  3. TRADEABILITY — the decisive test: buy the FAVORITE side (price > cutoff) at its ask, net of
     the Kalshi fee, swept across depth cutoffs. If favorites are underpriced this PROFITS
     (unlike the crypto case, where it lost). CI is EVENT-BLOCK bootstrapped (resample the RACE,
     not the candidate-market — the candidate YES-markets within one race sum to ~1 and are NOT
     independent; the honest unit is the event).

Decision-time price = the daily candle close at ~`--horizon-days` before the market's close
(genuine uncertainty; the closing price is near-deterministic and useless for calibration).

Usage:
    uv run python -m ml.research.politics_calibration
    uv run python -m ml.research.politics_calibration --days 400 --horizon-days 7 --max-markets 3000
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

from ingestion.kalshi import KalshiClient
from ml.alpha.backtest import _per_window_pnl
from ml.alpha.metrics import reliability_table, score

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
            o = decision_obs(client, st, m, horizon_days)
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
    no_ask = 1.0 - np.array([o.yes_bid for o in obs])
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

    print("\nTRADEABILITY — BUY THE FAVORITE (side priced > cutoff) at its ask, net of Kalshi fee:")
    print(f"  {'cutoff':>7}{'bets':>7}{'win%':>7}{'mean pnl/ct($)':>16}{'ROI':>9}"
          f"{'  event-block 95% CI ($/ct)':>28}")
    fav_is_yes = price >= 0.5
    for thr in FAVORITE_CUTOFFS:
        fav_prob = np.where(fav_is_yes, price, 1.0 - price)
        q = fav_prob > thr
        bet_yes = q & fav_is_yes
        bet_no = q & ~fav_is_yes
        pnl, staked = _per_window_pnl(bet_yes, bet_no, outcome, yes_ask, no_ask)
        sel = bet_yes | bet_no
        if sel.sum() < 8:
            print(f"  {thr:>7.2f}{int(sel.sum()):>7}   (too few)")
            continue
        sub_pnl = pnl[sel]
        sub_ev = [e for e, k in zip(events, sel, strict=True) if k]
        wins = ((bet_yes & (outcome == 1)) | (bet_no & (outcome == 0)))[sel]
        roi = sub_pnl.sum() / staked[sel].sum() if staked[sel].sum() else float("nan")
        point, lo, hi = _event_block_ci(sub_pnl, sub_ev, rng)
        print(f"  {thr:>7.2f}{int(sel.sum()):>7}{wins.mean():>7.0%}{sub_pnl.mean():>+16.4f}"
              f"{roi:>+9.2%}   [{lo:+.4f},{hi:+.4f}] ({len(set(sub_ev))}ev)")

    print("\nRead: ECE >> crypto's ~0.007 + a compression slope CI above 1 = the lit's politics")
    print("mispricing is present here. It's TRADEABLE only if a favorite-cutoff row shows mean")
    print("pnl/ct with an event-block CI ENTIRELY above 0 (clears spread + fee). A positive point")
    print("that straddles 0 = real bias, still trapped in friction — the usual wall.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
