"""Analyze the accumulated LP live-pilot logs — the honest feasibility read.

Reads data/lp_sessions.csv (one row per market quoted) + data/lp_fills.csv (one
row per fill with its 30s markout) and reports:

  * Realized P&L. Kalshi's own `kalshi_gross` is the authoritative realized number;
    `net_cash` can OVERSTATE it when a session ended with an un-flattened residual
    on a resolved market (the residual's settlement is directional luck, not edge),
    so we report both and flag the gap.
  * The edge signal: fill-weighted mean MARKOUT with a SESSION-BLOCK bootstrap CI
    (resample whole markets, since fills within a market are correlated). markout~0
    = no adverse selection => the P&L is genuine spread capture. markout<0 = the
    flow is picking us off.
  * What drives productivity: a breakdown by market TYPE (total/game/spread/match/
    mention/...) showing fills, $/min, and markout — i.e. WHERE the money is.

Usage:
    uv run python -m ml.lp.lp_analyze
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SESSION_LOG = "data/lp_sessions.csv"
FILL_LOG = "data/lp_fills.csv"
N_BOOT = 5000
ET = timezone(timedelta(hours=-4))  # for grouping sessions into calendar days


def _cfg(row: dict[str, Any]) -> str:
    """Config tag for a row; rows logged before the column existed read as 'legacy'."""
    return row.get("config_version") or "legacy"


def _et_day(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ET).date().isoformat()


def _rows(path: str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="") as fh:
        return list(csv.DictReader(fh))


def market_type(ticker: str) -> str:
    """Coarse type from the ticker so we can see which kinds of markets pay."""
    for key in ("TOTAL", "SPREAD", "MENTION", "RFI", "AST", "1H", "2H", "GAME", "MATCH"):
        if key in ticker:
            return key
    return "OTHER"


def bootstrap_ci(values: list[float], weights: list[float]) -> tuple[float, float]:
    """95% CI on the weighted mean, resampling INDEX BLOCKS (markets) with
    replacement. Here each element is already one market's (markout, n_fills)."""
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


def config_breakdown(sessions: list[dict[str, Any]], fills: list[dict[str, Any]]) -> None:
    """Per-ruleset summary so old-rules and new-rules runs are never blended into one
    verdict. Each fill inherits its session's config via the (ts_utc, market) key.
    Reports DAYS (the real sufficiency unit), markets, fills, net $, and markout+CI."""
    cfg_of = {(s["ts_utc"], s["market"]): _cfg(s) for s in sessions}
    mk_by_cfg: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for f in fills:
        if f.get("markout_c"):
            cfg = cfg_of.get((f["ts_utc"], f["market"]), "legacy")
            mk_by_cfg[cfg][f["market"]].append(float(f["markout_c"]))

    print("\n" + "=" * 72)
    print("BY CONFIG VERSION  (separate rulesets — never blend them for a verdict)")
    print("=" * 72)
    chdr = f"{'config':<20}{'days':>5}{'mkts':>5}{'fills':>7}{'net$':>9}{'markout':>9}"
    print(chdr + f"{'  95% CI':>17}")
    print("-" * 72)
    for cfg in sorted({_cfg(s) for s in sessions}):
        rows = [s for s in sessions if _cfg(s) == cfg]
        days = len({_et_day(s["ts_utc"]) for s in rows})
        mkts = len({s["market"] for s in rows})
        n_fills = sum(int(s["n_fills"]) for s in rows)
        net = sum(float(s["kalshi_gross"]) - float(s["fees"]) for s in rows)
        vals = [sum(v) / len(v) for v in mk_by_cfg[cfg].values()]
        wts = [float(len(v)) for v in mk_by_cfg[cfg].values()]
        tot = sum(wts)
        mk = sum(a * b for a, b in zip(vals, wts, strict=True)) / tot if tot else float("nan")
        lo, hi = bootstrap_ci(vals, wts)
        ci = f"[{lo:+.2f},{hi:+.2f}]" if lo == lo else "  n/a"
        print(f"{cfg:<20}{days:>5}{mkts:>5}{n_fills:>7}{net:>+9.2f}{mk:>+8.2f}c{ci:>17}")
    print("\nWatch the NEW config's DAY count climb — a strategy verdict needs ~tens of")
    print("days (markout CI clearing 0 under day-level resampling), not thousands of fills.")
    print("The 'legacy' rows are diagnostic-only: the current rules were DERIVED from them.")


def main() -> int:
    sessions = _rows(SESSION_LOG)
    fills = _rows(FILL_LOG)
    if not sessions:
        print("No sessions logged yet.")
        return 1

    n = len(sessions)
    total_min = sum(float(s["minutes"]) for s in sessions)
    total_fills = sum(int(s["n_fills"]) for s in sessions)
    gross = sum(float(s["kalshi_gross"]) for s in sessions)
    net_cash = sum(float(s["net_cash"]) for s in sessions)
    fees = sum(float(s["fees"]) for s in sessions)
    pos = sum(1 for s in sessions if float(s["kalshi_gross"]) > 0)
    neg = sum(1 for s in sessions if float(s["kalshi_gross"]) < 0)

    print("=" * 72)
    print("LP LIVE-PILOT ANALYSIS")
    print("=" * 72)
    print(f"markets quoted : {n}   over {total_min / 60:.1f} h   {total_fills} fills")
    print(f"REALIZED (Kalshi gross): ${gross:+.2f}   fees ${fees:.2f}   net ${gross - fees:+.2f}")
    print(f"net_cash (incl. residuals): ${net_cash:+.2f}   "
          f"<- gap ${net_cash - gross:+.2f} = unsettled/luck on resolved residuals")
    print(f"sessions positive/negative (by gross): {pos} / {neg}")

    config_breakdown(sessions, fills)

    # --- the edge signal: per-market markout, session-block bootstrap ----
    by_mkt_mark: dict[str, list[float]] = defaultdict(list)
    for f in fills:
        if f.get("markout_c"):
            by_mkt_mark[f["market"]].append(float(f["markout_c"]))
    mk_vals = [sum(v) / len(v) for v in by_mkt_mark.values()]  # per-market mean markout
    mk_wts = [float(len(v)) for v in by_mkt_mark.values()]  # weight by n_fills
    n_marked = sum(mk_wts)
    weighted = sum(m * w for m, w in zip(mk_vals, mk_wts, strict=True))
    fw_markout = weighted / n_marked if n_marked else float("nan")
    lo, hi = bootstrap_ci(mk_vals, mk_wts)
    print(f"\nFILL MARKOUT (the edge signal), fill-weighted: {fw_markout:+.3f}c/fill")
    print(f"  session-block 95% CI [{lo:+.3f}, {hi:+.3f}]c  (n={len(mk_vals)} markets, "
          f"{int(n_marked)} marked fills)")
    print("  >0 = no adverse selection (P&L is real spread capture); ~0/<0 = thin/none")

    # --- where the money is: by market type ------------------------------
    agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in sessions:
        t = market_type(s["market"])
        agg[t]["gross"] += float(s["kalshi_gross"])
        agg[t]["fills"] += int(s["n_fills"])
        agg[t]["min"] += float(s["minutes"])
        agg[t]["n"] += 1
    print(f"\n{'type':<10}{'mkts':>5}{'fills':>7}{'minutes':>9}{'$/min':>9}{'gross $':>10}")
    print("-" * 50)
    for t, a in sorted(agg.items(), key=lambda kv: kv[1]["gross"], reverse=True):
        dpm = a["gross"] / a["min"] if a["min"] else 0.0
        print(f"{t:<10}{int(a['n']):>5}{int(a['fills']):>7}{a['min']:>9.0f}{dpm:>+9.3f}{a['gross']:>+10.2f}")

    print("\nRead: productivity = fills (volume x duration) x per-fill edge; markout")
    print("must stay >=~0 or volume just churns the spread away. Concentration + a tiny")
    print("sample (n markets) mean WIDE CIs — this is a first look, not a verdict.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
