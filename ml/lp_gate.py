"""Selection gate for the LP maker + a counterfactual re-score of the logged night.

The 06-16/17 overnight run was flat-to-negative (realized -$0.88 net) but the loss
was NOT uniform: it was entirely low-tier ITF tennis and thin books. This module:

  1. Defines the GATE we'd apply at selection time (``passes_gate``) so the live bot
     and this re-score share ONE definition — exclude toxic ticker types, require a
     minimum recent-trade rate. ``pick_smooth_ticker`` imports it.
  2. Re-scores the ALREADY-LOGGED night (data/lp_sessions.csv + lp_fills.csv) under
     several candidate gates and reports the counterfactual realized P&L + markout,
     so we can see whether gating recovers the clean +$ the thick markets made.

Honesty note on look-ahead: the TYPE exclusion (e.g. drop ITF) is a pure ticker-string
filter — identical a-priori and retrospectively, zero leakage. The VOLUME floor here
is applied on each session's REALIZED fills/min, which is only known after quoting;
it is a proxy for the a-priori signal the live gate actually uses (recent trades/min
from the trades feed). So read the volume rows as "an upper bound on what a volume
floor buys," and the ITF row as the clean, directly-implementable result.

Usage:
    uv run python -m ml.lp_gate            # re-score the logged night under each gate
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

SESSION_LOG = "data/lp_sessions.csv"
FILL_LOG = "data/lp_fills.csv"
N_BOOT = 5000

# --- the GATE (shared with pick_smooth_ticker) ------------------------------
# Ticker substrings whose markets proved structurally toxic to quote: low-tier
# pro tennis (ITF World Tennis Tour $15-25k events) — thin books, the few
# counterparties are informed, markout runs negative. Excluded at selection.
TOXIC_TYPES: tuple[str, ...] = ("ITF",)
# Minimum recent trades the candidate must show (count of its trades in the live
# /markets/trades window) before we quote it — keeps us out of books too thin to
# capture spread without being picked off. Activity is the dominant edge driver:
# pooled across 58 sessions the per-fill edge is −0.56c in the THIN tercile vs
# +0.49c in the ACTIVE one (markout only washes to ~0 when flow is busy).
# Calibrated 06-17 against a live peak-hours probe: active markets (WC game/spread,
# MLB) sat at 15-77 recent trades, the bleeding thin tail (incl. that day's
# MLB/ATP losers) at 4-13 — a clean gap at ~14. NB the count's time-scale shifts
# (the 1000-trade window is ~12s at peak, much longer overnight), so it bites
# HARDEST during busy hours; overnight the ITF exclusion carries more of the load.
MIN_RECENT_TRADES = 15


def passes_gate(ticker: str, recent_trades: int) -> bool:
    """True if `ticker` is eligible to quote: not a toxic type AND showing enough
    recent flow. `recent_trades` = count of this market's trades in the live
    trades-feed window (a-priori observable, no look-ahead)."""
    if any(t in ticker for t in TOXIC_TYPES):
        return False
    return recent_trades >= MIN_RECENT_TRADES


# --- re-score plumbing ------------------------------------------------------
def _rows(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _bootstrap_ci(values: list[float], weights: list[float]) -> tuple[float, float]:
    """95% CI on the weighted mean, resampling whole MARKETS (block bootstrap, since
    fills within a market are correlated). Each element is one market's mean markout."""
    n = len(values)
    if n < 2:
        return float("nan"), float("nan")
    rng = random.Random(0)
    means = []
    for _ in range(N_BOOT):
        idx = [rng.randrange(n) for _ in range(n)]
        wsum = sum(weights[i] for i in idx)
        if wsum > 0:
            means.append(sum(values[i] * weights[i] for i in idx) / wsum)
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def _score(
    sessions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    keep: set[str],
) -> dict[str, Any]:
    """Aggregate realized P&L + block-bootstrap markout over the sessions whose
    ticker is in `keep`. (Ticker, not row, so re-quoted markets stay grouped.)"""
    s_keep = [s for s in sessions if s["market"] in keep]
    gross = sum(float(s["kalshi_gross"]) for s in s_keep)
    fees = sum(float(s["fees"]) for s in s_keep)
    n_fills = sum(int(s["n_fills"]) for s in s_keep)
    by_mkt: dict[str, list[float]] = defaultdict(list)
    for f in fills:
        if f["market"] in keep and f.get("markout_c"):
            by_mkt[f["market"]].append(float(f["markout_c"]))
    vals = [sum(v) / len(v) for v in by_mkt.values()]
    wts = [float(len(v)) for v in by_mkt.values()]
    tot_w = sum(wts)
    mk = sum(v * w for v, w in zip(vals, wts, strict=True)) / tot_w if tot_w else float("nan")
    lo, hi = _bootstrap_ci(vals, wts)
    return {
        "markets": len({s["market"] for s in s_keep}),
        "fills": n_fills,
        "gross": gross,
        "net": gross - fees,
        "markout": mk,
        "ci": (lo, hi),
    }


def main() -> int:
    sessions = _rows(SESSION_LOG)
    fills = _rows(FILL_LOG)
    if not sessions:
        print("No sessions logged yet.")
        return 1

    all_tk = {s["market"] for s in sessions}
    # per-ticker realized fills/min (max across re-quotes) for the volume-floor proxy
    fpm: dict[str, float] = defaultdict(float)
    for s in sessions:
        fpm[s["market"]] = max(fpm[s["market"]], float(s["fills_per_min"]))

    def not_toxic(tk: str) -> bool:
        return not any(t in tk for t in TOXIC_TYPES)

    gates: list[tuple[str, set[str]]] = [
        ("baseline (all)", all_tk),
        ("drop ITF", {tk for tk in all_tk if not_toxic(tk)}),
        ("fills/min >= 2.0", {tk for tk in all_tk if fpm[tk] >= 2.0}),
        ("fills/min >= 3.5", {tk for tk in all_tk if fpm[tk] >= 3.5}),
        ("drop ITF & fpm >= 2.0", {tk for tk in all_tk if not_toxic(tk) and fpm[tk] >= 2.0}),
        ("drop ITF & fpm >= 3.5", {tk for tk in all_tk if not_toxic(tk) and fpm[tk] >= 3.5}),
    ]

    print("=" * 86)
    print("COUNTERFACTUAL RE-SCORE — what each selection gate would have made on 06-16/17")
    print("=" * 86)
    hdr = f"{'gate':<24}{'mkts':>5}{'fills':>7}{'gross$':>9}{'net$':>9}{'markout':>9}"
    print(hdr + f"{'  95% CI':>18}")
    print("-" * 86)
    for label, keep in gates:
        r = _score(sessions, fills, keep)
        lo, hi = r["ci"]
        ci = f"[{lo:+.2f},{hi:+.2f}]" if lo == lo else "  n/a"
        print(
            f"{label:<24}{r['markets']:>5}{r['fills']:>7}{r['gross']:>+9.2f}"
            f"{r['net']:>+9.2f}{r['markout']:>+8.2f}c{ci:>18}"
        )
    print("-" * 86)
    print("net = realized Kalshi gross - fees. markout >0 = no adverse selection (clean")
    print("spread capture). 'drop ITF' is a pure ticker filter (no look-ahead); the fpm")
    print("rows use realized fills/min and so UPPER-BOUND what a live volume floor buys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
