"""W+13 cluster verdict — is the W+9..13 'profit cluster' real, or the same
latency-bound lead-lag artifact as W+1?

The per-minute sweep (ml/decision_minute_profit.py) found the spot-displacement
strategy's ROI clusters at W+9..W+13 (~+5-7% vs the backfill candle), but with two
fatal caveats: the CIs were +/-3-4% on ~41 OOS days, and we only had LIVE order
books at W+1 — so we could not check whether the W+12-13 book reprices (and
therefore eats the edge) the way the W+1 book does. The AWS Lambda collector
(lambda/orderbook_collector/) was deployed precisely to close that gap: it banks
the live executable touch + simultaneous BTC spot at ~W+2 / ~W+13 / ~W+15 of every
window, 24/7, to s3://$S3_BUCKET/raw/orderbook_snapshots/.

This script renders the verdict from those snapshots. Per captured-and-settled
window it builds three PRICINGS of the same displacement strategy at W+13:

  1. backfill candle   — decide AND fill at the W+13 candle close (exactly what the
                         sweep priced => should reproduce the cluster ROI here).
  2. fixed bets, live  — the SAME bets as (1), but filled at the live executable
                         touch captured ~W+12:51. Isolates pure fill-price effect.
  3. live replay       — what a real trader does: compute displacement from the
                         snapshot's own btc_spot, decide against the LIVE ask,
                         fill at the LIVE ask.

plus the model-free mechanism stats that discriminate the hypotheses:

  * side-conditional slippage: (live ask of the side the signal bets) - (candle ask
    of that side). The artifact story predicts mean > 0 — the book has ALREADY
    repriced in the direction of the spot move the candle close lags behind.
  * within-burst repricing speed + spot-tracking at W+13 vs W+2 (replicating the
    R^2=0.92 W+1 finding on ~16x the windows, and testing it near expiry).
  * W+15 book liveness (can you even trade near expiry?).

The displacement model is fit FORWARD: logistic on (disp, disp^2) trained on the
warehouse k=13 history STRICTLY BEFORE the first captured window (same protocol as
ml/live_paper_pnl.py), so every scored window is out-of-sample. Uncertainty is
day-block bootstrap (the honest unit is DAYS — see decision_minute_profit).

Read-only / public APIs + S3 already synced locally — no auth, no orders.

Usage:
    aws s3 sync s3://$S3_BUCKET/raw/orderbook_snapshots/ data/s3_orderbook_snapshots/
    uv run python -m ml.alpha.live_cluster_verdict
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
from dotenv import load_dotenv

from ingestion.coinbase import fetch_bars
from ingestion.kalshi import SERIES_BTC_15M, KalshiClient
from ml.alpha.backtest import _per_window_pnl
from ml.alpha.data import _athena_connection
from ml.alpha.decision_minute_profit import N_BOOT, RNG, _day_sums, _roi, _strategy_pnl
from ml.alpha.model import logistic_pipeline
from ml.alpha.settlement_lag import _load_at_k

SNAPSHOT_DIR = Path("data/s3_orderbook_snapshots")
DECISION_K = 13  # the cluster minute the Lambda captures actually land on (~W+12:51)
BURSTS = {"wk2": (60.0, 180.0), "wk13": (720.0, 840.0), "wk15": (840.0, 960.0)}
CAPTURED_KS = (2, 13)  # decision minutes with a live-book burst (run with: -m ... [k])

FloatArr = npt.NDArray[np.float64]
IntArr = npt.NDArray[np.intp]


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class WindowRec:
    """One captured + settled window with everything the three pricings need."""

    ticker: str
    open_at: datetime
    day: date
    y: int  # 1 = settled YES (up)
    bf_yes_bid: float  # W+13 candle close (what the sweep priced at)
    bf_yes_ask: float
    lv_yes_bid: float  # live executable touch nearest W+13:00
    lv_yes_ask: float
    disp_bf: float  # (candle close spot[W+13] - spot[W]) / spot[W]
    disp_lv: float  # (snapshot btc_spot - spot[W]) / spot[W]


# --- snapshot loading --------------------------------------------------------
def _load_snapshots() -> dict[str, list[dict[str, Any]]]:
    """All Lambda snapshot rows grouped by market ticker, time-ordered."""
    by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fp in sorted(SNAPSHOT_DIR.glob("dt=*/*.jsonl")):
        for line in fp.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                by_ticker[row["market_ticker"]].append(row)
    for rows in by_ticker.values():
        rows.sort(key=lambda r: r["captured_at"])
    return by_ticker


def _burst_rows(
    rows: list[dict[str, Any]], open_at: datetime, lo_s: float, hi_s: float
) -> list[dict[str, Any]]:
    """Two-sided snapshots captured lo_s..hi_s seconds after the window open."""
    out = []
    for r in rows:
        off = (_iso(r["captured_at"]) - open_at).total_seconds()
        if lo_s <= off <= hi_s and r.get("mid") is not None:
            out.append(r)
    return out


# --- mechanism stats ---------------------------------------------------------
def _burst_dynamics(
    by_ticker: dict[str, list[dict[str, Any]]], lo_s: float, hi_s: float
) -> dict[str, FloatArr]:
    """Per window: mid drift per 20s across the burst + paired (d_spot, d_mid) for
    the spot-tracking regression. Mirrors live_exec_reconcile Part 2/2b."""
    drift, abs_drift, d_spot, d_mid = [], [], [], []
    for rows in by_ticker.values():
        open_at = _iso(rows[0]["window_open_at"])
        burst = _burst_rows(rows, open_at, lo_s, hi_s)
        if len(burst) < 2:
            continue
        t0, t1 = _iso(burst[0]["captured_at"]), _iso(burst[-1]["captured_at"])
        secs = (t1 - t0).total_seconds()
        if secs <= 0:
            continue
        move = float(burst[-1]["mid"]) - float(burst[0]["mid"])
        drift.append(move / secs * 20.0)
        abs_drift.append(abs(move) / secs * 20.0)
        spotted = [r for r in burst if r.get("btc_spot") is not None]
        if len(spotted) >= 2:
            d_spot.append(float(spotted[-1]["btc_spot"]) - float(spotted[0]["btc_spot"]))
            d_mid.append((float(spotted[-1]["mid"]) - float(spotted[0]["mid"])) * 100.0)
    return {
        "drift": np.array(drift),
        "abs_drift": np.array(abs_drift),
        "d_spot": np.array(d_spot),
        "d_mid": np.array(d_mid),
    }


def _print_dynamics(label: str, dyn: dict[str, FloatArr]) -> None:
    n = len(dyn["drift"])
    if n == 0 or len(dyn["d_spot"]) < 3:
        print(f"{label}: insufficient burst data (n={n})")
        return
    slope, _ = np.polyfit(dyn["d_spot"], dyn["d_mid"], 1)
    r = float(np.corrcoef(dyn["d_spot"], dyn["d_mid"])[0, 1])
    drift_c, abs_c = np.mean(dyn["drift"]) * 100, np.mean(dyn["abs_drift"]) * 100
    print(
        f"{label:<8}{n:>6}{drift_c:>+9.2f}{abs_c:>9.2f}{slope * 100:>+11.2f}{r:>+7.2f}{r * r:>7.2f}"
    )


# --- bootstrap ---------------------------------------------------------------
def _day_ci(pnl: FloatArr, stake: FloatArr, days: npt.NDArray[np.object_]) -> tuple[float, float]:
    """Day-block bootstrap 95% CI on ROI (same protocol as decision_minute_profit)."""
    day = _day_sums(pnl, stake, days)
    dp, ds = day["pnl"].to_numpy(), day["stake"].to_numpy()
    n = len(dp)
    rois = []
    for _ in range(N_BOOT):
        idx = RNG.integers(0, n, n)
        s = ds[idx].sum()
        if s > 0:
            rois.append(dp[idx].sum() / s)
    arr = np.array(rois)
    return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))


def _paired_diff_ci(
    pnl_a: FloatArr,
    stake_a: FloatArr,
    pnl_b: FloatArr,
    stake_b: FloatArr,
    days: npt.NDArray[np.object_],
) -> tuple[float, float]:
    """Day-block bootstrap 95% CI on (ROI_a - ROI_b), resampling the SAME days for
    both legs — the paired comparison the small day-count actually supports."""
    df = (
        pd.DataFrame({"day": days, "pa": pnl_a, "sa": stake_a, "pb": pnl_b, "sb": stake_b})
        .groupby("day", as_index=False)
        .sum()
    )
    n = len(df)
    diffs = []
    for _ in range(N_BOOT):
        idx = RNG.integers(0, n, n)
        sa, sb = df["sa"].to_numpy()[idx].sum(), df["sb"].to_numpy()[idx].sum()
        if sa > 0 and sb > 0:
            diffs.append(df["pa"].to_numpy()[idx].sum() / sa - df["pb"].to_numpy()[idx].sum() / sb)
    arr = np.array(diffs)
    return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))


def _pnl_with_fills(
    bet_yes: npt.NDArray[np.bool_],
    bet_no: npt.NDArray[np.bool_],
    y: IntArr,
    yes_ask: FloatArr,
    yes_bid: FloatArr,
) -> tuple[FloatArr, FloatArr]:
    """PnL/stake for a FIXED bet set filled at the given quote (fee included) —
    the 'same bets, repriced' leg that _strategy_pnl (which re-decides) can't do."""
    return _per_window_pnl(bet_yes, bet_no, y, yes_ask, 1.0 - yes_bid)


# --- assembly ----------------------------------------------------------------
def _assemble_windows(  # noqa: PLR0914 — one pass, many aligned pieces
    by_ticker: dict[str, list[dict[str, Any]]], k: int
) -> tuple[list[WindowRec], dict[str, int]]:
    """Join snapshots + Kalshi settlements/candles + Coinbase spot into WindowRecs."""
    opens = [_iso(rows[0]["window_open_at"]) for rows in by_ticker.values()]
    closes = [_iso(rows[0]["window_close_at"]) for rows in by_ticker.values()]
    lo, hi = min(opens), max(closes)

    client = KalshiClient()
    try:
        settled = {
            m["ticker"]: m
            for m in client.list_markets(
                SERIES_BTC_15M,
                status="settled",
                min_close_ts=int(lo.timestamp()) - 60,
                max_close_ts=int(hi.timestamp()) + 60,
            )
        }
        # Candles batched under Kalshi's request budget: the batch endpoint caps
        # n_tickers x range_minutes at ~10k candles, so chunk consecutive windows
        # greedily instead of one whole day (96 tickers x 1,456 min => 400).
        candles: dict[str, list[dict[str, Any]]] = {}
        windows = sorted(
            (ticker, _iso(rows[0]["window_open_at"]), _iso(rows[0]["window_close_at"]))
            for ticker, rows in by_ticker.items()
        )
        chunk: list[tuple[str, datetime, datetime]] = []
        for w in [*windows, None]:  # trailing None sentinel flushes the last chunk
            if w is not None:
                trial = [*chunk, w]
                span_min = (max(t[2] for t in trial) - trial[0][1]).total_seconds() / 60 + 2
                if not chunk or (len(trial) <= 100 and len(trial) * span_min <= 9000):
                    chunk = trial
                    continue
            if chunk:
                candles.update(
                    client.get_market_candlesticks_batch(
                        [t[0] for t in chunk],
                        int(chunk[0][1].timestamp()) - 60,
                        int(max(t[2] for t in chunk).timestamp()) + 60,
                    )
                )
            chunk = [w] if w is not None else []
    finally:
        client.close()

    bars = {b.event_at: b for b in fetch_bars("BTC-USD", lo - timedelta(minutes=1), hi)}

    drops = {"unsettled": 0, "no_candle": 0, "no_live": 0, "no_spot": 0}
    recs: list[WindowRec] = []
    for ticker, rows in sorted(by_ticker.items()):
        open_at = _iso(rows[0]["window_open_at"])
        market = settled.get(ticker)
        if market is None or market.get("result") not in ("yes", "no"):
            drops["unsettled"] += 1
            continue

        # Backfill quote + spot-close at the decision candle (end == W+k:00).
        target_end = int(open_at.timestamp()) + k * 60
        cdl = next(
            (c for c in candles.get(ticker, []) if int(c["end_period_ts"]) == target_end), None
        )
        yb = ((cdl or {}).get("yes_bid") or {}).get("close_dollars")
        ya = ((cdl or {}).get("yes_ask") or {}).get("close_dollars")
        if yb is None or ya is None:
            drops["no_candle"] += 1
            continue

        burst = _burst_rows(rows, open_at, *BURSTS[f"wk{k}"])
        live = [r for r in burst if r.get("btc_spot") is not None]
        if not live:
            drops["no_live"] += 1
            continue
        target_t = open_at + timedelta(seconds=k * 60)
        dec = min(live, key=lambda r: abs((_iso(r["captured_at"]) - target_t).total_seconds()))

        # Spot at the window open (Coinbase minute-bar open at W) and the candle-close
        # spot at W+k (close of the bar starting W+k-1) — fct_features_pit's convention.
        bar_open = bars.get(open_at)
        bar_k = bars.get(open_at + timedelta(minutes=k - 1))
        if bar_open is None or bar_k is None:
            drops["no_spot"] += 1
            continue

        recs.append(
            WindowRec(
                ticker=ticker,
                open_at=open_at,
                day=open_at.date(),
                y=1 if market["result"] == "yes" else 0,
                bf_yes_bid=float(yb),
                bf_yes_ask=float(ya),
                lv_yes_bid=float(dec["best_yes_bid"]),
                lv_yes_ask=float(dec["best_yes_ask"]),
                disp_bf=(bar_k.close - bar_open.open) / bar_open.open,
                disp_lv=(float(dec["btc_spot"]) - bar_open.open) / bar_open.open,
            )
        )
    return recs, drops


def main() -> int:
    load_dotenv()
    k = int(sys.argv[1]) if len(sys.argv) > 1 else DECISION_K
    if k not in CAPTURED_KS:
        print(f"FAIL: k={k} has no live-book burst; captured ks: {CAPTURED_KS}", file=sys.stderr)
        return 1
    if not SNAPSHOT_DIR.exists():
        print(f"FAIL: {SNAPSHOT_DIR} not found — run the aws s3 sync first.", file=sys.stderr)
        return 1

    by_ticker = _load_snapshots()
    print(f"Lambda snapshot windows captured: {len(by_ticker)}")
    recs, drops = _assemble_windows(by_ticker, k)
    if not recs:
        print(f"FAIL: no usable windows (drops: {drops})", file=sys.stderr)
        return 1
    n_days = len({r.day for r in recs})
    print(
        f"Usable settled windows with W+{k} candle + live book + spot: "
        f"{len(recs)} over {n_days} days   (drops: {drops})\n"
    )

    # --- forward-fit the displacement model on warehouse history before the test ---
    first_test = pd.Timestamp(min(r.open_at for r in recs))
    hist = _load_at_k(_athena_connection().cursor(), k)
    hist["window_open_at"] = pd.to_datetime(hist["window_open_at"], utc=True)
    train = hist[hist["window_open_at"] < first_test]
    xt = np.column_stack([train["disp_ret"].to_numpy(), train["disp_ret"].to_numpy() ** 2])
    model = logistic_pipeline().fit(xt, train["y"].to_numpy())
    print(
        f"Displacement model: logistic(disp, disp^2) at k={k}, trained on "
        f"{len(train):,} warehouse windows strictly before {first_test.date()} "
        f"(out-of-sample by construction).\n"
    )

    y = np.array([r.y for r in recs], dtype=np.intp)
    days = np.asarray([r.day for r in recs], dtype=object)
    disp_bf = np.array([r.disp_bf for r in recs])
    disp_lv = np.array([r.disp_lv for r in recs])
    prob_bf = model.predict_proba(np.column_stack([disp_bf, disp_bf**2]))[:, 1]
    prob_lv = model.predict_proba(np.column_stack([disp_lv, disp_lv**2]))[:, 1]
    bf_ask = np.clip(np.array([r.bf_yes_ask for r in recs]), 0.0, 1.0)
    bf_bid = np.clip(np.array([r.bf_yes_bid for r in recs]), 0.0, 1.0)
    lv_ask = np.clip(np.array([r.lv_yes_ask for r in recs]), 0.0, 1.0)
    lv_bid = np.clip(np.array([r.lv_yes_bid for r in recs]), 0.0, 1.0)

    # --- the three pricings of the same strategy -----------------------------
    bet_yes_bf, bet_no_bf = (prob_bf - bf_ask) > 0.0, (bf_bid - prob_bf) > 0.0
    legs: list[tuple[str, FloatArr, FloatArr]] = []

    pnl_bf, stake_bf = _strategy_pnl(prob_bf, y, bf_ask, bf_bid)
    legs.append(("1 backfill candle (the sweep's #)", pnl_bf, stake_bf))
    pnl_fx, stake_fx = _pnl_with_fills(bet_yes_bf, bet_no_bf, y, lv_ask, lv_bid)
    legs.append(("2 same bets, LIVE fills", pnl_fx, stake_fx))
    pnl_lv, stake_lv = _strategy_pnl(prob_lv, y, lv_ask, lv_bid)
    legs.append(("3 live replay (live disp+fill)", pnl_lv, stake_lv))
    # Controls: follow-the-move with no model, both pricings.
    pnl_nbf, stake_nbf = _pnl_with_fills(disp_bf > 0, disp_bf < 0, y, bf_ask, bf_bid)
    legs.append(("naive follow-move @ candle", pnl_nbf, stake_nbf))
    pnl_nlv, stake_nlv = _pnl_with_fills(disp_lv > 0, disp_lv < 0, y, lv_ask, lv_bid)
    legs.append(("naive follow-move @ LIVE", pnl_nlv, stake_nlv))

    print(
        f"Displacement strategy at W+{k}, three pricings "
        f"(threshold-0 rule, net of spread+fee; day-block bootstrap, n_days={n_days}):"
    )
    print(f"{'pricing':<34}{'bets':>6}{'win%':>8}{'PnL($)':>9}{'ROI':>9}{'95% CI':>20}")
    print("-" * 86)
    for label, pnl, stake in legs:
        bets = int((stake > 0).sum())
        wins = int(((pnl > 0) & (stake > 0)).sum())
        lo_ci, hi_ci = _day_ci(pnl, stake, days)
        win = wins / bets if bets else float("nan")
        print(
            f"{label:<34}{bets:>6}{win:>8.1%}{pnl.sum():>+9.2f}{_roi(pnl, stake):>+9.1%}"
            f"{f'[{lo_ci:+.1%}, {hi_ci:+.1%}]':>20}"
        )

    dlo, dhi = _paired_diff_ci(pnl_bf, stake_bf, pnl_fx, stake_fx, days)
    diff = _roi(pnl_bf, stake_bf) - _roi(pnl_fx, stake_fx)
    print(
        f"\nPaired execution cost (leg1 ROI - leg2 ROI, same bets, same days): "
        f"{diff:+.1%}  95% CI [{dlo:+.1%}, {dhi:+.1%}]"
    )

    # --- mechanism: side-conditional slippage --------------------------------
    bet = bet_yes_bf | bet_no_bf
    slip = np.where(bet_yes_bf, lv_ask - bf_ask, np.where(bet_no_bf, bf_bid - lv_bid, np.nan))
    s = slip[bet] * 100.0
    if s.size:
        print(
            f"\nSide-conditional slippage on leg-1's {s.size} bets "
            f"(live ask of bet side - candle ask, cents):"
        )
        print(
            f"  mean {s.mean():+.2f}   median {np.median(s):+.2f}   std {s.std():.2f}   "
            f"adverse (>0): {np.mean(s > 0):.0%}"
        )
        print(
            "  (artifact mechanism predicts mean > 0: the live book has already repriced "
            "toward the move\n   the candle close lags behind.)"
        )

    # --- mechanism: repricing dynamics by burst ------------------------------
    print(
        "\nWithin-burst repricing dynamics (per window; drift in cents/20s; "
        "mid-vs-spot regression):"
    )
    print(f"{'burst':<8}{'n':>6}{'drift':>9}{'|drift|':>9}{'c/$100':>11}{'r':>7}{'R^2':>7}")
    print("-" * 57)
    for name in ("wk2", "wk13"):
        _print_dynamics(name, _burst_dynamics(by_ticker, *BURSTS[name]))

    # --- W+15 book liveness ----------------------------------------------------
    n15 = alive15 = 0
    for rows in by_ticker.values():
        open_at = _iso(rows[0]["window_open_at"])
        lo_s, hi_s = BURSTS["wk15"]
        for r in rows:
            off = (_iso(r["captured_at"]) - open_at).total_seconds()
            if lo_s <= off <= hi_s:
                n15 += 1
                alive15 += r.get("mid") is not None
    if n15:
        print(
            f"\nW+15 (last ~minute) book liveness: {alive15}/{n15} snapshots two-sided "
            f"({alive15 / n15:.0%}) — near expiry the book mostly empties (no entry OR exit)."
        )

    print(
        "\nHow to read the verdict:\n"
        "  * leg 1 >> legs 2-3 with positive side-slippage and W+13 spot-tracking ~ W+2's\n"
        "    => the cluster is the SAME latency-bound lead-lag artifact, now shown at W+13\n"
        "       with live books (the prior). Cluster CLOSED.\n"
        "  * legs 2-3 positive with CIs clear of 0 => the cluster survives live pricing —\n"
        "    keep accruing days before believing it (n_days here is still small)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
