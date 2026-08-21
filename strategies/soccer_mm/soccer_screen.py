"""Does the World-Cup market-making microstructure REPEAT in year-round club soccer?

The one surviving edge is MM on Kalshi in-play soccer TOTAL/SPREAD books: wide retail spreads
+ a benign (rare-discrete-scoring => mean-reverting) price process + maker-free fills. WC is
seasonal; this screens whether the SAME setup exists in the summer-calendar club leagues (MLS,
Brasileirão, Liga MX, Scandinavia) so the edge can run all year.

This is Stage 1 — the spread + liquidity HALF of the thesis, readable from the live /markets
snapshot with no price history: are the near-money TOTAL/SPREAD buckets in the 2-15c retail band,
and is there real two-sided volume to fill against? WC is screened alongside as the calibrated
benchmark. The other half — is the in-play process actually benign (mean-reverting, not toxic
pick-off)? — needs a live-game flow log (core.capture.ws_logger during a match); this
does not test it.

Read-only, no auth. Numbers are a snapshot; run during evening games (US) when club books are live.

Usage:
    uv run python -m strategies.soccer_mm.soccer_screen
    uv run python -m strategies.soccer_mm.soccer_screen --leagues WC,MLS,BRA,MEX
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from statistics import median
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import KalshiClient

# league -> the TOTAL/SPREAD series that carry the edge (GAME = moneyline, less mean-reverting)
LEAGUES: dict[str, list[str]] = {
    "WC": ["KXWCTOTAL", "KXWCSPREAD"],            # benchmark: the proven-profitable cell
    "MLS": ["KXMLSTOTAL", "KXMLSSPREAD"],
    "BRA": ["KXBRASILEIROTOTAL", "KXBRASILEIROSPREAD"],
    "MEX": ["KXLIGAMXTOTAL", "KXLIGAMXSPREAD"],
    "ARG": ["KXBUNDESLIGATOTAL", "KXBUNDESLIGASPREAD"],  # placeholder off-season leagues, if open
}
NEAR_LO, NEAR_HI = 0.15, 0.85  # "near money" = a real two-sided quote, not a settled 1c tail


@dataclass
class MktRow:
    series: str
    spread_c: float
    mid: float
    vol_24h: float
    oi: float
    active: bool


def _f(m: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = m.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def screen_series(client: KalshiClient, series: str) -> list[MktRow]:
    """Near-money, two-sided TOTAL/SPREAD markets for one series from the live snapshot."""
    try:
        markets = client.list_markets(series, status="open")
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] {series}: {str(exc)[:70]}", file=sys.stderr)
        return []
    rows: list[MktRow] = []
    for m in markets:
        bid = _f(m, "yes_bid_dollars")
        ask = _f(m, "yes_ask_dollars")
        if bid is None or ask is None or bid <= 0 or ask >= 1 or ask <= bid:
            continue  # one-sided / empty / crossed -> not a quote we could rest inside
        mid = (bid + ask) / 2
        if not (NEAR_LO <= mid <= NEAR_HI):
            continue
        vol = _f(m, "volume_24h_fp", "volume_fp") or 0.0
        rows.append(MktRow(series, (ask - bid) * 100, mid, vol,
                           _f(m, "open_interest_fp") or 0.0, m.get("status") == "active"))
    return rows


def report(league: str, rows: list[MktRow]) -> dict[str, float]:
    n = len(rows)
    if not n:
        print(f"{league:<5}{'—':>6}   no near-money two-sided TOTAL/SPREAD markets open right now")
        return {"n": 0}
    spreads = [r.spread_c for r in rows]
    vols = [r.vol_24h for r in rows]
    active = sum(r.active for r in rows)
    in_band = sum(2 <= s <= 15 for s in spreads)  # the retail-spread band the WC edge lives in
    r = {
        "n": float(n), "active": float(active),
        "med_spread": float(median(spreads)), "med_vol": float(median(vols)),
        "tot_vol": float(sum(vols)), "in_band_frac": in_band / n,
    }
    print(f"{league:<5}{n:>6}{active:>8}{r['med_spread']:>11.1f}c{in_band / n:>9.0%}"
          f"{r['med_vol']:>11,.0f}{r['tot_vol']:>13,.0f}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description="Screen club-soccer MM microstructure vs WC benchmark")
    ap.add_argument("--leagues", default="WC,MLS,BRA,MEX", help=f"from {sorted(LEAGUES)}")
    args = ap.parse_args()

    load_dotenv()
    client = KalshiClient()
    leagues = [x.strip().upper() for x in args.leagues.split(",") if x.strip()]
    print("Screening near-money in-play TOTAL/SPREAD books (WC = the proven-profitable benchmark).")
    print("Edge needs BOTH: near-money spread in ~2-15c retail band AND real two-sided volume.\n")
    print(f"{'lg':<5}{'mkts':>6}{'active':>8}{'medSpr':>12}{'in-band':>9}{'medVol':>11}{'totVol':>13}")
    print("-" * 64)
    results: dict[str, dict[str, float]] = {}
    try:
        for lg in leagues:
            if lg not in LEAGUES:
                print(f"[skip] unknown league {lg}", file=sys.stderr)
                continue
            rows: list[MktRow] = []
            for series in LEAGUES[lg]:
                rows.extend(screen_series(client, series))
            results[lg] = report(lg, rows)
    finally:
        client.close()

    bench = results.get("WC", {})
    print("\nRead: compare each league to WC. A league with a WC-like near-money spread (~2-4c),\n"
          "a high in-band fraction, and real volume has the SPREAD half of the edge — worth a\n"
          "live in-play flow log (core.capture.ws_logger --prefix <SERIES> --minutes 30\n"
          "in a match) to\n"
          "test the BENIGN-PROCESS half (mean-reversion vs pick-off) before quoting. Thin volume\n"
          "or 1c spreads = no room to make; wide (>15c) usually = an empty book, not opportunity.")
    if bench.get("n"):
        print(f"\n(WC benchmark this snapshot: med spread {bench['med_spread']:.1f}c, "
              f"median 24h vol {bench['med_vol']:,.0f}/market.)")
    else:
        print("\n(WC has no live near-money markets this snapshot — the final is Jul 19; "
              "compare leagues to the memory baseline: WC near-money ~2-4c, +0.59c/fill.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
