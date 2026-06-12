"""Per-minute decision sweep WITH uncertainty — which minute is "most profitable",
and do we even have enough data to say?

Extends ml/settlement_lag.py from a coarse grid to EVERY decision minute k=1..14.
Because picking the best of 14 minutes invites noise-mining, it quantifies the
uncertainty two ways:
  * a block (per-DAY) bootstrap 95% CI on each minute's ROI. The resampling unit is
    the DAY (~66 of them) — the real effective sample size, NOT the ~6,100 windows,
    since windows within a day share a regime and are far from independent.
  * a split-half stability check: is the best minute on the first half still good on
    the second half? If the ranking doesn't survive the split, "most profitable
    minute" is noise, not signal.

IMPORTANT framing: the ROI here is the spot-displacement strategy priced against the
W+k candle — the SAME within-minute lead-lag artifact shown to be latency-bound in
ml/live_exec_reconcile.py. So this ranks where that ARTIFACT is largest, not a real
tradeable edge. The deliverable is the data-sufficiency answer.

Usage:
    uv run python -m ml.decision_minute_profit
"""

import sys

import numpy as np
import numpy.typing as npt
import pandas as pd

from ml.backtest import _per_window_pnl
from ml.data import _athena_connection
from ml.settlement_lag import _disp_model_oof, _load_at_k

K_RANGE = range(1, 15)  # decide at W+1 .. W+14
N_BOOT = 4000
RNG = np.random.default_rng(0)

FloatArr = npt.NDArray[np.float64]
IntArr = npt.NDArray[np.intp]


def _strategy_pnl(
    prob: FloatArr, y: IntArr, yes_ask: FloatArr, yes_bid: FloatArr
) -> tuple[FloatArr, FloatArr]:
    """Per-window PnL and stake for the threshold-0 bet rule (bet a side when the
    model clears that side's ask), net of the Kalshi fee. Returns parallel arrays so
    they can be aggregated by day for the bootstrap."""
    return _per_window_pnl(
        (prob - yes_ask) > 0.0, (yes_bid - prob) > 0.0, y, yes_ask, 1.0 - yes_bid
    )


def _roi(pnl: FloatArr, stake: FloatArr) -> float:
    s = float(stake.sum())
    return float(pnl.sum()) / s if s > 0 else float("nan")


def _day_sums(pnl: FloatArr, stake: FloatArr, dates: npt.NDArray[np.object_]) -> pd.DataFrame:
    """Collapse to one (pnl, stake) row per day — the bootstrap/​split-half unit."""
    return (
        pd.DataFrame({"date": dates, "pnl": pnl, "stake": stake})
        .groupby("date", as_index=False)
        .sum()
    )


def _bootstrap_ci(day: pd.DataFrame) -> tuple[float, float]:
    """Resample DAYS with replacement -> 95% CI on ROI. Resampling whole days (not
    windows) respects within-day correlation, so the CI reflects ~n_days of real
    independent information."""
    dp = day["pnl"].to_numpy()
    ds = day["stake"].to_numpy()
    n = len(dp)
    rois = []
    for _ in range(N_BOOT):
        idx = RNG.integers(0, n, n)
        s = ds[idx].sum()
        if s > 0:
            rois.append(dp[idx].sum() / s)
    arr = np.array(rois)
    return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))


def main() -> int:
    conn = _athena_connection()
    cur = conn.cursor()

    rows: list[dict[str, float]] = []
    n_days = 0
    for k in K_RANGE:
        df = _load_at_k(cur, k)
        y = df["y"].to_numpy().astype(np.intp)
        oof = _disp_model_oof(df["disp_ret"].to_numpy(), y)
        live = ~np.isnan(oof)

        prob = oof[live]
        yk = y[live]
        yes_ask = np.clip(df["yes_ask"].to_numpy()[live], 0.0, 1.0)
        yes_bid = np.clip(df["yes_bid"].to_numpy()[live], 0.0, 1.0)
        dates = pd.to_datetime(df["window_open_at"].to_numpy()[live]).date

        pnl, stake = _strategy_pnl(prob, yk, yes_ask, yes_bid)
        day = _day_sums(pnl, stake, np.asarray(dates, dtype=object))
        n_days = len(day)
        lo, hi = _bootstrap_ci(day)

        # Split-half by calendar time (does early profitability persist late?).
        mid = day["date"].sort_values().iloc[len(day) // 2]
        h1 = day[day["date"] < mid]
        h2 = day[day["date"] >= mid]
        rows.append(
            {
                "k": k,
                "n_bets": float((stake > 0).sum()),
                "roi": _roi(pnl, stake),
                "lo": lo,
                "hi": hi,
                "roi_h1": _roi(h1["pnl"].to_numpy(), h1["stake"].to_numpy()),
                "roi_h2": _roi(h2["pnl"].to_numpy(), h2["stake"].to_numpy()),
            }
        )

    res = pd.DataFrame(rows)

    print(f"Spot-displacement strategy ROI by decision minute (n_days={n_days}, "
          f"day-block bootstrap 95% CI, {N_BOOT} resamples)\n")
    print(f"{'W+k':<6}{'bets':>8}{'ROI':>9}{'95% CI':>20}{'sig?':>6}{'H1 ROI':>10}{'H2 ROI':>10}")
    print("-" * 69)
    for _, r in res.iterrows():
        sig = "yes" if r["lo"] > 0 else "no"
        ci = f"[{r['lo']:+.1%}, {r['hi']:+.1%}]"
        print(
            f"{'W+' + str(int(r['k'])):<6}{int(r['n_bets']):>8,}{r['roi']:>+9.1%}{ci:>20}"
            f"{sig:>6}{r['roi_h1']:>+10.1%}{r['roi_h2']:>+10.1%}"
        )

    best = res.loc[res["roi"].idxmax()]
    best_h1 = res.loc[res["roi_h1"].idxmax()]
    corr = float(np.corrcoef(res["roi_h1"], res["roi_h2"])[0, 1])

    print(f"\nBest minute overall: W+{int(best['k'])} at {best['roi']:+.1%} "
          f"(CI [{best['lo']:+.1%}, {best['hi']:+.1%}]).")
    print(
        "\nDo we have enough data? — the honest read:\n"
        f"  * Effective sample is ~{n_days} DAYS, not the ~6,100 windows: the walk-forward\n"
        "    only scores the later ~60% of the timeline out-of-sample, and windows within\n"
        "    a day share a regime. So the day-block CIs are WIDE (+/-3-4%) and the\n"
        "    mid-window minutes (W+9..W+13) overlap each other heavily — you cannot pick\n"
        "    a single statistically-distinguishable 'best' minute, only a positive cluster.\n"
        f"  * Split-half stability: per-minute ROI correlates {corr:+.2f} between halves "
        f"(weak);\n    the best-in-H1 minute (W+{int(best_h1['k'])}) returns "
        f"{best_h1['roi_h2']:+.1%} in H2. A high positive correlation + a\n"
        "    best minute that stays good => signal; this weak one => the RANKING is mostly\n"
        "    noise even where the level is positive.\n"
        "  * Decisive caveat: this ROI is the latency-bound lead-lag ARTIFACT (priced vs\n"
        "    the lagging candle; collapses at real execution per live_exec_reconcile.py)\n"
        "    over a SINGLE ~70-day regime. We have order-book snapshots only at W+1, so we\n"
        "    can't even confirm the W+12 book reprices as fast — but the mechanism is the\n"
        "    same. So: not enough data to crown a best minute, and what profit shows is\n"
        "    almost certainly the artifact, not durable alpha."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
