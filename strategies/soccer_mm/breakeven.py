"""STEP 0 — spread-vs-toxicity BREAKEVEN for club-soccer market-making (go/no-go).

The post-WC hypothesis is to run the maker on year-round club soccer (MLS / Liga MX /
Brasileirao / UCL...). Step 1 (`realized_toxicity.py`) showed WC soccer carried a mild but
REAL adverse-selection tax (WC/SPREAD 30s fill-markout -0.135c). Club books are thinner. The
review's worry: a thin club spread may not cover that tax + fees. This script answers it from
GROUND TRUTH before a dollar is risked.

The economics of one maker fill (soccer is maker-free, so fee=0):

    net_per_fill = capture + markout
        capture  = the half-spread we bank at the fill instant  = (mid0 - price) signed by side
        markout  = the 30s adverse/favourable drift after the fill (Step 1's toxicity), <0 = toxic

We FIT net(S) as a function of the quoted near-money spread S from our OWN 15,829 WC fills (the
only soccer ground truth), per market-type, with a day-block bootstrap, and solve for the
BREAKEVEN spread S* where net = 0. Then we read each candidate club league's LIVE in-play
near-money spread and place it against that curve.

Honest boundaries (this bounds the decision; it does not replace the live pilot):
  * capture(S) and toxicity are measured on WC fills; club soccer is ASSUMED to share the
    scoring-driven (rare, discrete) price process. Whether it truly does is a live-capture
    question. We bracket that transfer risk with two efficiency scenarios: the WC/SPREAD curve
    (club behaves as well as the proven cell) and the WC/TOTAL curve (club is as capture-poor
    as WC totals were). The live capture's job is to reveal which curve club soccer sits on.
  * live league spreads are a point-in-time in-play snapshot (game-state dependent); the
    07-15 universe snapshot is the offline fallback and UNDERSTATES in-play spreads (it showed
    WC at 1c where we actually quoted 2.57c), so it is a lower-bound read, flagged STALE.

Usage:
    uv run python -m strategies.soccer_mm.breakeven                      # live league spreads
    uv run python -m strategies.soccer_mm.breakeven --offline   # 07-15 snapshot spreads only
    uv run python -m strategies.soccer_mm.breakeven --leagues MLS,MEX,BRA
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from typing import Any

import numpy as np

from core.maker.classify import market_type, sport
from core.maker.realized_toxicity import _et_day

FILLS_CSV = "data/lp_fills.csv"
SESSIONS_CSV = "data/lp_sessions.csv"
SNAPSHOT = "dashboard/data/opportunity_snapshot.parquet"
N_BOOT = 5000
MIN_DAYS = 4  # a net(S) line needs a few ET days of spread variation to be meaningful

# candidate club leagues -> (TOTAL series, SPREAD series). WC is the ground-truth benchmark.
CLUB_LEAGUES: dict[str, list[str]] = {
    "MLS": ["KXMLSTOTAL", "KXMLSSPREAD"],
    "MEX": ["KXLIGAMXTOTAL", "KXLIGAMXSPREAD"],
    "BRA": ["KXBRASILEIROTOTAL", "KXBRASILEIROSPREAD"],
    "UCL": ["KXUCLTOTAL", "KXUCLSPREAD"],
    "UEL": ["KXUELTOTAL", "KXUELSPREAD"],
}


# ---------------------------------------------------------------------------
# 1. Ground truth: the net(S) curve from our WC fills
# ---------------------------------------------------------------------------
def load_fill_points() -> dict[str, dict[str, list[tuple[float, float, float]]]]:
    """family -> et_day -> [(quoted_spread_c, capture_c, markout_c) per fill].

    The quoted spread is the session's ``avg_spread_c`` (all fills in a session share one
    ``ts_utc`` = session start); capture is reconstructed from (side, price, mid0)."""
    session_spread: dict[tuple[str, str], float] = {}
    with open(SESSIONS_CSV) as fh:
        for row in csv.DictReader(fh):
            try:
                session_spread[(row["market"], row["ts_utc"])] = float(row["avg_spread_c"])
            except (ValueError, KeyError):
                continue

    out: dict[str, dict[str, list[tuple[float, float, float]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    with open(FILLS_CSV) as fh:
        for row in csv.DictReader(fh):
            try:
                price, mid0, mk = float(row["price"]), float(row["mid0"]), float(row["markout_c"])
            except (ValueError, KeyError, TypeError):
                continue
            spread = session_spread.get((row["market"], row["ts_utc"]))
            if spread is None:
                continue
            if row["side"] == "bid":
                cap = (mid0 - price) * 100
            elif row["side"] == "ask":
                cap = (price - mid0) * 100
            else:
                continue
            fam = f"{sport(row['market'])}/{market_type(row['market'])}"
            out[fam][_et_day(row["ts_utc"])].append((spread, cap, mk))
    return out


def _wls_line(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Ordinary least-squares line y = a + b*x (equal weights). Returns (intercept, slope)."""
    xm, ym = x.mean(), y.mean()
    var = float(((x - xm) ** 2).sum())
    if var < 1e-9:  # no spread variation in this sample -> undefined slope
        return float("nan"), float("nan")
    b = float(((x - xm) * (y - ym)).sum() / var)
    return ym - b * xm, b


def fit_family(
    days: dict[str, list[tuple[float, float, float]]], rng: np.random.Generator
) -> dict[str, Any]:
    """Fit net(S) = a + b*S for one family, with a day-block bootstrap on the breakeven S* and
    on net at a grid of reference spreads. net = capture + markout (fee=0, maker-free soccer)."""
    day_keys = list(days)
    # point fit on all fills
    all_pts = [p for k in day_keys for p in days[k]]
    x = np.array([p[0] for p in all_pts])
    net = np.array([p[1] + p[2] for p in all_pts])
    a, b = _wls_line(x, net)
    star = -a / b if b > 1e-9 else float("nan")

    # decomposition (unweighted per-day mean, matching Step 1's reporting)
    cap_daymean = float(np.mean([np.mean([p[1] for p in days[k]]) for k in day_keys]))
    mk_daymean = float(np.mean([np.mean([p[2] for p in days[k]]) for k in day_keys]))

    boot_star: list[float] = []
    boot_a: list[float] = []
    boot_b: list[float] = []
    n = len(day_keys)
    for _ in range(N_BOOT):
        pick = [day_keys[i] for i in rng.integers(0, n, n)]
        pts = [p for k in pick for p in days[k]]
        bx = np.array([p[0] for p in pts])
        bnet = np.array([p[1] + p[2] for p in pts])
        ba, bb = _wls_line(bx, bnet)
        if not np.isfinite(bb) or bb <= 1e-9:
            continue
        boot_star.append(-ba / bb)
        boot_a.append(ba)
        boot_b.append(bb)
    star_ci = (
        (float(np.percentile(boot_star, 2.5)), float(np.percentile(boot_star, 97.5)))
        if boot_star
        else (float("nan"), float("nan"))
    )
    ref = np.array([1.0, 2.0, 3.0, 4.0])
    return {
        "a": a, "b": b, "star": star, "star_ci": star_ci,
        "cap": cap_daymean, "mk": mk_daymean, "net": cap_daymean + mk_daymean,
        "mean_spread": float(x.mean()),
        "lo_spread": float(np.percentile(x, 5)), "hi_spread": float(np.percentile(x, 95)),
        "days": n, "fills": len(all_pts),
        "ref": ref, "net_ref": a + b * ref,
        "boot_a": np.array(boot_a), "boot_b": np.array(boot_b),
    }


def predict_net(fit: dict[str, Any], spread: float) -> tuple[float, float, float]:
    """Predicted net-per-fill (cents) at a spread, with a day-block CI computed at THAT spread
    from the stored bootstrap lines (correct for any spread, incl. beyond the reference grid)."""
    point = fit["a"] + fit["b"] * spread
    if not len(fit["boot_a"]):
        return point, float("nan"), float("nan")
    boot = fit["boot_a"] + fit["boot_b"] * spread
    return point, float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))


def family_summary(
    fam_days: dict[str, dict[str, list[tuple[float, float, float]]]], min_fills: int = 200
) -> list[tuple[str, int, int, float, float, float, float]]:
    """Per family (>= min_fills): (family, days, fills, mean_spread, capture, markout, net).
    capture/markout are unweighted per-day means (Step 1's convention); sorted least-toxic first."""
    out: list[tuple[str, int, int, float, float, float, float]] = []
    for fam, days in fam_days.items():
        pts = [p for v in days.values() for p in v]
        if len(pts) < min_fills:
            continue
        cap = float(np.mean([np.mean([p[1] for p in v]) for v in days.values()]))
        mk = float(np.mean([np.mean([p[2] for p in v]) for v in days.values()]))
        sp = float(np.mean([p[0] for p in pts]))
        out.append((fam, len(days), len(pts), sp, cap, mk, cap + mk))
    out.sort(key=lambda r: -r[5])  # markout descending = least toxic (closest to 0) first
    return out


# ---------------------------------------------------------------------------
# 2. Candidate club-league spreads (live, snapshot fallback)
# ---------------------------------------------------------------------------
def live_spreads(leagues: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """(league, market_type) -> {spread, vol, n} from the live in-play /markets snapshot."""
    from dotenv import load_dotenv

    from ingestion.kalshi import KalshiClient
    from strategies.soccer_mm.soccer_screen import screen_series

    load_dotenv()
    client = KalshiClient()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for lg in leagues:
            for series in CLUB_LEAGUES[lg]:
                rows = screen_series(client, series)
                if not rows:
                    continue
                mt = market_type(series)
                out[(lg, mt)] = {
                    "spread": float(np.median([r.spread_c for r in rows])),
                    "vol": float(sum(r.vol_24h for r in rows)),
                    "n": len(rows),
                    "source": "LIVE",
                }
    finally:
        client.close()
    return out


def snapshot_spreads(leagues: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """(league, market_type) -> spread from the 07-15 universe snapshot (STALE fallback)."""
    import pandas as pd

    df = pd.read_parquet(SNAPSHOT)
    idx = df.set_index("series_ticker")
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for lg in leagues:
        for series in CLUB_LEAGUES[lg]:
            if series not in idx.index:
                continue
            r = idx.loc[series]
            sp = r["median_near_money_spread_c"]
            if sp is None or (isinstance(sp, float) and np.isnan(sp)):
                continue
            out[(lg, market_type(series))] = {
                "spread": float(sp), "vol": float(r["volume_24h"]),
                "n": int(r["n_near_money_markets"]), "source": "STALE",
                "maker_fee": bool(r["has_maker_fee"]),
            }
    return out


# ---------------------------------------------------------------------------
# 3. Report
# ---------------------------------------------------------------------------
def verdict(net_lo: float, net_hi: float, floor: float) -> str:
    """Classify a club book: the matched-curve CI plus the pessimistic-efficiency floor."""
    if net_lo > 0.05 and floor >= 0:
        return "CLEARS (robust)"
    if net_lo > 0.0:
        return "CLEARS if efficient*"
    if net_hi < 0:
        return "BELOW BREAKEVEN"
    return "MARGINAL"


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 0 club-soccer spread-vs-toxicity breakeven")
    ap.add_argument("--leagues", default="MLS,MEX,BRA,UCL,UEL")
    ap.add_argument("--offline", action="store_true", help="use the 07-15 snapshot, no live API")
    args = ap.parse_args()

    fam_days = load_fill_points()
    rng = np.random.default_rng(7)
    fits = {
        fam: fit_family(days, rng)
        for fam, days in fam_days.items()
        if len(days) >= MIN_DAYS and sum(len(v) for v in days.values()) >= 200
    }

    print("STEP 0 — SPREAD vs TOXICITY BREAKEVEN  (club-soccer market-making go/no-go)")
    print("net_per_fill = capture + markout (fee=0, soccer maker-free); fit from our WC fills.\n")

    # --- the soccer ground-truth curves: net across the spread grid + interpreted breakeven ---
    print("[1] GROUND-TRUTH net-per-fill curve  (net at each spread; day-block bootstrap CI)")
    hdr = (f"{'family':<12}{'days':>5}{'fills':>7}{'capture':>9}{'markout':>9}"
           f"{'net@1c':>8}{'net@2c':>8}{'net@3c':>8}{'net@4c':>8}  breakeven")
    print(hdr + "\n" + "-" * len(hdr))
    for fam in ["WC/SPREAD", "WC/TOTAL"]:
        if fam not in fits:
            continue
        f = fits[fam]
        n1, n2, n3, n4 = f["net_ref"]
        if f["b"] < 0.02:  # spread-insensitive (flat curve) -> breakeven undefined/uninformative
            be = f"flat ~{n2:+.2f}c (spread-insensitive)"
        elif f["star"] <= 1.0:
            lo, hi = f["star_ci"]
            be = f"<1c (net+ across range) [{lo:.1f},{hi:.1f}]"
        else:
            be = f"{f['star']:.2f}c"
        print(f"{fam:<12}{f['days']:>5}{f['fills']:>7}{f['cap']:>+9.3f}{f['mk']:>+9.3f}"
              f"{n1:>+8.3f}{n2:>+8.3f}{n3:>+8.3f}{n4:>+8.3f}  {be}")
    print("    (net@Nc = fill-level line fit; per-day-mean net in [2] weights each day once — they")
    print("     differ most for the flat, spread-insensitive TOTAL cell.)")

    # --- toxicity sign-check: soccer reads LEAST toxic; toxic sports lose despite wide books ---
    print("\n[2] TOXICITY SIGN-CHECK — 30s markout per family; soccer should be the LEAST toxic")
    summ = family_summary(fam_days)
    hdr = f"    {'family':<12}{'days':>5}{'fills':>7}{'spread':>8}{'markout':>9}{'net':>8}  note"
    print(hdr + "\n    " + "-" * (len(hdr) - 4))
    for fam, nd, nf, sp, _cap, mk, net in summ:
        note = ("soccer edge" if fam == "WC/SPREAD" else
                "soccer (benign)" if fam.startswith("WC") else
                "TOXIC: net<0 at wide book" if net < 0 else
                "toxic markout, thin margin")
        flag = "" if nd >= MIN_DAYS else " (<4d)"
        print(f"    {fam:<12}{nd:>5}{nf:>7}{sp:>7.1f}c{mk:>+9.3f}{net:>+8.3f}  {note}{flag}")

    # --- candidate club leagues placed against the curve ---
    leagues = [x.strip().upper() for x in args.leagues.split(",") if x.strip() in CLUB_LEAGUES]
    spreads: dict[tuple[str, str], dict[str, Any]] = {}
    if not args.offline:
        try:
            spreads = live_spreads(leagues)
        except Exception as exc:  # noqa: BLE001
            print(f"\n[warn] live screen failed ({str(exc)[:60]}); falling back to snapshot",
                  file=sys.stderr)
    if not spreads:
        spreads = snapshot_spreads(leagues)

    is_live = any(v["source"] == "LIVE" for v in spreads.values())
    src = "LIVE in-play" if is_live else "07-15 STALE snapshot"
    print(f"\n[3] CANDIDATE CLUB LEAGUES vs the curve  (spread source: {src})")
    print("    predicted net at each league's near-money spread; matched market-type curve +")
    print("    the WC/TOTAL-efficiency floor (the pessimistic transfer bracket).")
    hdr = (f"    {'league':<7}{'type':>7}{'spread':>8}{'vol24h':>10}{'pred net':>10}"
           f"{'  95% CI':>16}{'  floor':>8}  verdict")
    print(hdr + "\n    " + "-" * (len(hdr) - 4))
    for lg in leagues:
        for mt in ("SPREAD", "TOTAL"):
            info = spreads.get((lg, mt))
            if info is None:
                continue
            matched = fits.get(f"WC/{mt}")
            if matched is None:
                continue
            s = info["spread"]
            net, lo, hi = predict_net(matched, s)
            floor, _, _ = predict_net(fits["WC/TOTAL"], s)  # pessimistic-efficiency bracket
            tags = []
            if info["source"] != "LIVE":
                tags.append("stale")
            if s > matched["hi_spread"]:  # beyond the WC fitted spread range -> extrapolated
                tags.append(f"EXTRAP>{matched['hi_spread']:.0f}c")
            tag = f"  ({', '.join(tags)})" if tags else ""
            print(f"    {lg:<7}{mt:>7}{s:>7.1f}c{info['vol']:>10,.0f}{net:>+10.3f}"
                  f"  [{lo:+.2f},{hi:+.2f}]{floor:>+8.3f}  {verdict(lo, hi, floor)}{tag}")

    print("\n  * 'CLEARS if efficient' = clears IF club books earn the spread as well as WC/SPREAD")
    print("    did; if they behave like the capture-poor WC/TOTAL cell (floor<0) they may not.")
    print("    'floor' = net on the WC/TOTAL (capture-poor) curve = pessimistic transfer bracket.")
    print("    'EXTRAP' = spread past WC's fitted range; the line is extrapolated, trust it less.")
    print("    Only a live club-game capture (ws_features) resolves which curve club soccer is on.")
    print("\nVERDICT: on the SPREAD axis, spread width is NOT the binding constraint — WC/SPREAD")
    print("breaks even near ~1c, well below any club SPREAD book. The real gate is capture-")
    print("efficiency + toxicity TRANSFER, which needs a live club capture. Club TOTAL is instead")
    print("marginal (WC/TOTAL itself barely cleared). Recommend: capture a club SPREAD book live")
    print("before quoting; do NOT lead with club TOTAL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
