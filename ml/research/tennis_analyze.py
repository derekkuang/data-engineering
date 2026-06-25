"""Tennis overreaction analysis — does a tennis binary REVERT or CONTINUE after a jump?

Reads data/tennis_book.csv (mid series) for GENUINE tennis markets (prefix filter — the
logger's substring match caught esports false positives like 'VITFAL' ~ ITF) and tests the
sign of the price autocorrelation around jumps:

    jump = mid(t) - mid(t-BACK)     # the recent move (e.g. a break of serve)
    fwd  = mid(t+FWD) - mid(t)       # the subsequent move

corr(jump, fwd) < 0 => OVERREACTION (fade the jump); > 0 => MOMENTUM (follow). The edge is
only tradable if the conditional reversion EXCEEDS the spread you must cross to fade.

Mids are resampled to a fixed STEP grid (forward-filled, with a staleness cap) so the
irregular poll cadence doesn't bias the horizons.

Usage: uv run python -m ml.research.tennis_analyze
"""

from __future__ import annotations

import bisect
import csv
from collections import defaultdict
from datetime import datetime

import numpy as np

BOOK = "data/tennis_book.csv"
TENNIS_PREFIX = ("KXITF", "KXATP", "KXWTA", "KXTENNIS")
STEP = 15.0  # resample grid seconds
MAXSTALE = 30.0  # drop a grid point if the last real tick is older than this
BACK_S, FWD_S = 30.0, 60.0  # jump window / forward window


def main() -> int:
    rows = [r for r in csv.DictReader(open(BOOK)) if r["market"].startswith(TENNIS_PREFIX)]
    by: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for r in rows:
        by[r["market"]].append(
            (datetime.fromisoformat(r["ts_utc"]).timestamp(), float(r["mid"]), float(r["spread_c"]))
        )
    print("=" * 70)
    print("TENNIS OVERREACTION ANALYSIS (genuine tennis only)")
    print("=" * 70)
    print(f"book ticks (tennis): {len(rows)}   markets: {len(by)}")

    jb, jf = int(BACK_S / STEP), int(FWD_S / STEP)
    jumps: list[float] = []
    fwds: list[float] = []
    fwd2: list[float] = []  # forward at 2x horizon (decay check)
    spreads: list[float] = []
    for series in by.values():
        if len(series) < 8:
            continue
        series.sort()
        ts = [x[0] for x in series]
        mids = [x[1] for x in series]
        sps = [x[2] for x in series]
        n = int((ts[-1] - ts[0]) / STEP) + 1
        grid = [ts[0] + STEP * k for k in range(n)]
        gm: list[float | None] = []
        for g in grid:
            i = bisect.bisect_right(ts, g) - 1
            gm.append(mids[i] if (i >= 0 and g - ts[i] <= MAXSTALE) else None)
        for k in range(jb, n - 2 * jf):
            a, b, c, d = gm[k - jb], gm[k], gm[k + jf], gm[k + 2 * jf]
            if a is None or b is None or c is None:
                continue
            jumps.append((b - a) * 100)
            fwds.append((c - b) * 100)
            fwd2.append(((d - b) * 100) if d is not None else float("nan"))
            si = bisect.bisect_right(ts, grid[k]) - 1
            spreads.append(sps[si] if si >= 0 else float("nan"))

    j = np.array(jumps)
    f = np.array(fwds)
    f2 = np.array(fwd2)
    sp = np.array(spreads)
    print(f"jump/fwd samples: {len(j)}   median spread {np.nanmedian(sp):.1f}c\n")
    if len(j) < 50:
        print("Too few samples for a read.")
        return 0

    print(f"corr(jump, fwd@{FWD_S:.0f}s)  = {np.corrcoef(j, f)[0, 1]:+.3f}   "
          f"(<0 = overreaction/fade, >0 = momentum)")
    print(f"corr(jump, fwd@{2 * FWD_S:.0f}s) = "
          f"{np.corrcoef(j[~np.isnan(f2)], f2[~np.isnan(f2)])[0, 1]:+.3f}\n")

    print("CONDITIONAL — after a jump of size X, the mean subsequent move:")
    print(f"{'jump bucket':<14}{'n':>6}{'mean fwd':>10}{'reverts?':>10}{'> spread?':>11}")
    buckets = [(-99, -3, "<= -3c"), (-3, -1, "-3..-1c"), (-1, 1, "-1..1c"),
               (1, 3, "1..3c"), (3, 99, ">= 3c")]
    for lo, hi, lbl in buckets:
        m = (j >= lo) & (j < hi)
        if m.sum() < 10:
            continue
        mf = f[m].mean()
        msp = np.nanmedian(sp[m])
        jmid = (lo + hi) / 2 if abs(lo) < 90 and abs(hi) < 90 else (lo if lo > 0 else hi)
        reverts = "yes" if (jmid != 0 and np.sign(mf) == -np.sign(jmid)) else "no"
        beats = "yes" if abs(mf) > msp else "no"
        print(f"{lbl:<14}{int(m.sum()):>6}{mf:>+9.2f}c{reverts:>10}{beats:>11}")

    big = np.abs(j) >= 3
    if big.sum() >= 10:
        signed_rev = -np.sign(j[big]) * f[big]  # >0 = move reverted (toward pre-jump)
        msp = float(np.nanmedian(sp[big]))
        verdict = ">" if signed_rev.mean() > msp else "<="
        print(f"\nAfter big jumps (|jump|>=3c, n={int(big.sum())}): mean signed reversion "
              f"{signed_rev.mean():+.2f}c over {FWD_S:.0f}s vs median spread {msp:.1f}c "
              f"-> reversion {verdict} spread")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
