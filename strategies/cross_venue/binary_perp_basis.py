"""Binary <-> perpetual basis: does KXBTC15M lag KXBTCPERP, or price it consistently?

Kalshi launched BTC perpetual futures (KXBTCPERP) on 2026-06-03. The perp and the
KXBTC15M binary settle on the SAME reference (CF Benchmarks BRTI), so the perp is
a live, liquid, fee-free-for-now, continuously-quoted proxy for the binary's
settlement index. That is exactly the instrument the sub-minute "BRTI tick race"
thread always lacked (see [[project-benchmark-eda-finding]]): previously the only
spot proxy was Coinbase (a DIFFERENT index, and not tradeable against the binary).

The economic link: KXBTC15M "yes" pays $1 iff BRTI rises over the 15-min window.
So inside a window the perp DISPLACEMENT d_k = (perp[W+k] - perp[W0]) drives
P(up). This script asks the only question that matters for an edge:

  Is the binary's quoted price already CONSISTENT with where the perp has moved,
  or does the binary LAG the perp (in which case the perp-implied probability
  beats the binary quote and a cost-aware fade is tradeable)?

Two measurements, per decision minute k in {1,5,10,13}:

  1. CONSISTENCY (log loss head-to-head). Fit a walk-forward logistic of the
     outcome on the perp return r_k (out-of-fold, trains strictly on earlier
     windows). Compare its log loss to the binary mid's log loss on the same
     rows. If perp-implied << binary, the binary is the worse forecaster -> it
     lags the perp. PRIOR (from the closed settlement-lag work, which used the
     Coinbase proxy): the market already incorporates observable displacement,
     so we expect ~parity. Re-testing with the ACTUAL index + a tradeable hedge
     is the point.
  2. TRADEABILITY (cost-aware). Bet the perp-implied side whenever it clears the
     binary's ask/bid by a threshold; flat 1-contract stake, real Kalshi fee
     (reuses core.backtest.backtest money-math). Day-block bootstrap CI + no-skill controls.

Plus a minute-level lead-lag cross-correlation (perp return -> next binary move
vs the reverse). True sub-minute lead-lag needs tick capture (a collector
extension); minute candles can only see the coarse version.

All data is pulled live from the PUBLIC Kalshi API (perp market data lives under
/margin, no auth) over the perp's short lifetime, so the effective sample is only
~10 DAYS -- CIs are wide and reported honestly.

Usage:
    uv run python -m strategies.cross_venue.binary_perp_basis                 # last ~10 days
    uv run python -m strategies.cross_venue.binary_perp_basis --start 2026-06-04 --end 2026-06-14
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from core.backtest.backtest import _per_window_pnl, _summarise
from core.backtest.model import logistic_pipeline, walk_forward_oof
from ingestion.kalshi import SERIES_BTC_15M, KalshiClient

PERP_TICKER = "KXBTCPERP"
CONTRACT_SIZE = 0.0001  # KXBTCPERP price (dollars/contract) = BTC index * 0.0001
DECISION_MINUTES = (1, 5, 10, 13)
WINDOW_MINUTES = 15
N_BOOT = 4000
RNG = np.random.default_rng(0)

FloatArr = npt.NDArray[np.float64]
IntArr = npt.NDArray[np.intp]


# --- data -------------------------------------------------------------------
def load_binary_windows(
    client: KalshiClient, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Settled KXBTC15M windows with their per-minute implied-prob + quote path.

    Returns dicts: open_ts, y (1=up), and path {minute_offset: (mid, yes_bid, yes_ask)}.
    """
    markets = client.list_markets(
        SERIES_BTC_15M,
        status="settled",
        min_close_ts=int(start.timestamp()),
        max_close_ts=int(end.timestamp()),
    )
    out: list[dict[str, Any]] = []
    for m in markets:
        if m.get("result") not in ("yes", "no"):
            continue
        open_ts = int(datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
        candles = client.get_market_candlesticks(
            SERIES_BTC_15M, m["ticker"], open_ts - 60, open_ts + WINDOW_MINUTES * 60 + 60, 1
        )
        path: dict[int, tuple[float, float, float]] = {}
        for c in candles:
            offset = (int(c["end_period_ts"]) - 60 - open_ts) // 60  # minute offset from open
            # Event-contract candlesticks quote dollars under *_dollars keys
            # (unlike the perp, which uses plain `close`).
            price = c.get("price") or {}
            mid = price.get("close_dollars")
            yb = (c.get("yes_bid") or {}).get("close_dollars")
            ya = (c.get("yes_ask") or {}).get("close_dollars")
            mid_v: float
            if mid is not None:
                mid_v = float(mid)
            elif yb is not None and ya is not None:
                mid_v = (float(yb) + float(ya)) / 2
            else:
                continue  # no usable price this minute
            yb_v = float(yb) if yb is not None else mid_v
            ya_v = float(ya) if ya is not None else mid_v
            path[offset] = (mid_v, yb_v, ya_v)
        out.append({"open_ts": open_ts, "y": int(m["result"] == "yes"), "path": path})
    return out


def load_perp_minutes(client: KalshiClient, start: datetime, end: datetime) -> dict[int, float]:
    """{minute_start_ts: perp last price} pulled per-day to keep ranges tight."""
    perp: dict[int, float] = {}
    day = start
    while day < end:
        nxt = min(day + timedelta(days=1), end)
        cs = client.get_margin_candlesticks(
            PERP_TICKER, int(day.timestamp()) - 120, int(nxt.timestamp()) + 120, 1
        )
        for c in cs:
            close = (c.get("price") or {}).get("close")
            if close is not None:
                perp[int(c["end_period_ts"]) - 60] = float(close)
        day = nxt
    return perp


# --- feature frame at decision minute k -------------------------------------
def frame_at_k(windows: list[dict[str, Any]], perp: dict[int, float], k: int) -> pd.DataFrame:
    """One row per window: outcome, perp return at W+k, binary mid/quote at W+k."""
    rows = []
    for w in windows:
        o = w["open_ts"]
        p0, pk = perp.get(o), perp.get(o + k * 60)
        if p0 is None or pk is None or p0 == 0 or k not in w["path"]:
            continue
        mid, yb, ya = w["path"][k]
        rows.append(
            {
                "date": datetime.fromtimestamp(o, UTC).date(),
                "y": w["y"],
                "perp_ret": (pk - p0) / p0,
                "binary_mid": mid,
                "yes_bid": yb,
                "yes_ask": ya,
            }
        )
    return pd.DataFrame(rows)


# --- stats ------------------------------------------------------------------
def log_loss(p: FloatArr, y: IntArr) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def bootstrap_roi_ci(
    pnl: FloatArr, stake: FloatArr, dates: npt.NDArray[Any]
) -> tuple[float, float]:
    """Day-block bootstrap 95% CI on ROI (resample whole days — the real unit)."""
    day = (
        pd.DataFrame({"date": dates, "pnl": pnl, "stake": stake})
        .groupby("date", as_index=False)
        .sum()
    )
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


def minute_lead_lag(
    windows: list[dict[str, Any]], perp: dict[int, float]
) -> tuple[float, float, int]:
    """Pooled cross-correlations across all windows/minutes:
    (perp_leads, binary_leads, n_pairs).
      perp_leads  = corr(perp_ret[t],   binary_change[t+1])
      binary_leads= corr(binary_change[t], perp_ret[t+1])
    """
    perp_ret_lead, bin_chg_next, bin_chg_lead, perp_ret_next = [], [], [], []
    for w in windows:
        o = w["open_ts"]
        for t in range(1, WINDOW_MINUTES - 1):
            p_tm, p_t, p_tp = (
                perp.get(o + (t - 1) * 60),
                perp.get(o + t * 60),
                perp.get(o + (t + 1) * 60),
            )
            if (
                p_tm is None
                or p_t is None
                or p_tp is None
                or t - 1 not in w["path"]
                or t not in w["path"]
                or t + 1 not in w["path"]
            ):
                continue
            pr_t = (p_t - p_tm) / p_tm
            pr_tp = (p_tp - p_t) / p_t
            bc_t = w["path"][t][0] - w["path"][t - 1][0]
            bc_tp = w["path"][t + 1][0] - w["path"][t][0]
            perp_ret_lead.append(pr_t)
            bin_chg_next.append(bc_tp)
            bin_chg_lead.append(bc_t)
            perp_ret_next.append(pr_tp)
    if len(perp_ret_lead) < 30:
        return float("nan"), float("nan"), len(perp_ret_lead)
    perp_leads = float(np.corrcoef(perp_ret_lead, bin_chg_next)[0, 1])
    binary_leads = float(np.corrcoef(bin_chg_lead, perp_ret_next)[0, 1])
    return perp_leads, binary_leads, len(perp_ret_lead)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=str, default=None)
    ap.add_argument("--end", type=str, default=None)
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()
    if args.start and args.end:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    else:
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=args.days)

    print(f"Window: {start:%Y-%m-%d} -> {end:%Y-%m-%d} UTC  (perp launched 2026-06-03)\n")
    client = KalshiClient(pace_seconds=0.2)
    print("Loading binary windows + per-minute paths (this paginates many calls) ...")
    windows = load_binary_windows(client, start, end)
    print(f"  {len(windows)} settled KXBTC15M windows")
    print("Loading perp minute prices ...")
    perp = load_perp_minutes(client, start, end)
    print(f"  {len(perp)} perp minute bars")
    client.close()

    print("\n" + "=" * 82)
    print("CONSISTENCY (does perp add info BEYOND the binary?) + TRADEABILITY")
    print("=" * 82)
    print("  LL mkt   = OOF logit on [binary_mid]         (recalibrated market)")
    print("  LL +perp = OOF logit on [binary_mid, perp_ret]  (market + perp displacement)")
    print("  dLL < 0  => perp adds info the binary lacks (binary lags); ~0 => consistent")
    print(
        "  ROI      = cost-aware bet of the PERP-only signal vs the binary quote (its best shot)\n"
    )
    print(
        f"{'W+k':<6}{'n':>6}{'LL mkt':>9}{'LL +perp':>10}{'dLL':>9}"
        f"{'bet ROI':>10}{'day-block 95% CI':>22}"
    )
    print("-" * 82)

    for k in DECISION_MINUTES:
        df = frame_at_k(windows, perp, k)
        if len(df) < 50:
            print(f"W+{k:<4}{len(df):>6}  (too few rows)")
            continue
        y = df["y"].to_numpy().astype(np.intp)
        mid_col = df["binary_mid"].to_numpy().reshape(-1, 1)
        perp_col = df["perp_ret"].to_numpy().reshape(-1, 1)

        # Three out-of-fold models on the SAME walk-forward splits (same live mask):
        #   perp-only (for the bet), market-only, and market+perp (incremental test).
        oof_perp = walk_forward_oof(perp_col, y, logistic_pipeline, n_splits=8)
        oof_m1 = walk_forward_oof(mid_col, y, logistic_pipeline, n_splits=8)
        oof_m2 = walk_forward_oof(np.hstack([mid_col, perp_col]), y, logistic_pipeline, n_splits=8)
        live = ~np.isnan(oof_perp)

        yk = y[live]
        ll_m1 = log_loss(oof_m1[live], yk)
        ll_m2 = log_loss(oof_m2[live], yk)
        d_ll = ll_m2 - ll_m1  # negative => perp_ret improves on the market alone

        yes_ask = np.clip(df["yes_ask"].to_numpy()[live], 0.0, 1.0)
        yes_bid = np.clip(df["yes_bid"].to_numpy()[live], 0.0, 1.0)
        dates = df["date"].to_numpy()[live]
        perp_p = oof_perp[live]

        # cost-aware: bet the perp-implied side when it clears the binary quote.
        bet_yes = (perp_p - yes_ask) > 0.0
        bet_no = (yes_bid - perp_p) > 0.0
        summ = _summarise(bet_yes, bet_no, yk, yes_ask, 1.0 - yes_bid)
        pnl, stake = _per_window_pnl(bet_yes, bet_no, yk, yes_ask, 1.0 - yes_bid)
        lo, hi = bootstrap_roi_ci(pnl, stake, dates)

        ci = f"[{lo:+.1%}, {hi:+.1%}]"
        roi = summ["roi"]
        roi_s = f"{roi:+.1%}" if not np.isnan(roi) else "  n/a"
        print(
            f"W+{k:<4}{int(live.sum()):>6}{ll_m1:>9.4f}{ll_m2:>10.4f}{d_ll:>+9.4f}"
            f"{roi_s:>10}{ci:>22}"
        )

    # lead-lag
    perp_leads, binary_leads, n_pairs = minute_lead_lag(windows, perp)
    print(f"\nMinute-level lead-lag (pooled across windows, n_pairs={n_pairs:,}):")
    print(f"  corr(perp_ret[t], binary_change[t+1])  = {perp_leads:+.3f}   <- perp leads binary")
    print(f"  corr(binary_change[t], perp_ret[t+1])  = {binary_leads:+.3f}   <- binary leads perp")

    print("\nRead:")
    print("  * dLL ~ 0 => adding the perp displacement does NOT improve on the binary")
    print("    price alone: the binary already incorporates the perp; consistent, no lag,")
    print("    no edge (confirms the settlement-lag prior with the ACTUAL index). dLL < 0")
    print("    AND a bet-ROI CI above 0 => the binary lags the perp and the fade is")
    print("    tradeable after fees.")
    print("  * Lead-lag at MINUTE resolution is coarse; a real sub-minute perp->binary")
    print("    race needs a collector capturing both books tick-by-tick. n_days is ~10,")
    print("    so CIs are wide -- treat any single positive cell as a hint, not a verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
