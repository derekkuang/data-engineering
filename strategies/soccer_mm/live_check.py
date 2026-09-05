"""Is a big-five game IN PLAY right now with makeable SPREAD/TOTAL markets? (READ-ONLY, $0)

The go/no-go check before a live pilot. Kalshi soccer books only populate once a game is
IN PLAY — pre-game they list but sit unquoted — so "are there fixtures today" is NOT the
question; "is a two-sided SPREAD/TOTAL book quoted right now" is.

IMPORTANT (easy to get wrong by hand): the makeable markets live on DIFFERENT series from
the 3-way match result. `KX<LEAGUE>GAME` carries only Home/Away/Tie (directional, jump-toxic,
never quoted by our bot). The mean-reverting types the maker actually quotes are on
`KX<LEAGUE>TOTAL` / `KX<LEAGUE>SPREAD` (plus 1H/TEAM variants). The pilot's
`--prefix KXLALIGA` is a startswith match, so it correctly spans all of them.

Usage::

    uv run python -m strategies.soccer_mm.live_check                 # one-shot GO/WAIT
    uv run python -m strategies.soccer_mm.live_check --watch         # poll until a game is live
    uv run python -m strategies.soccer_mm.live_check --prefix KXLALIGA
"""

from __future__ import annotations

import argparse
import datetime as dt
import time
from dataclasses import dataclass

import httpx

KALSHI = "https://external-api.kalshi.com/trade-api/v2"

# The MAKEABLE series (mean-reverting TOTAL/SPREAD), per league. Deliberately excludes
# KX<LEAGUE>GAME (3-way match result) — directional, jump-toxic, never quoted by the maker.
MAKEABLE_SERIES: dict[str, tuple[str, ...]] = {
    "LALIGA": ("KXLALIGATOTAL", "KXLALIGASPREAD", "KXLALIGA1HTOTAL", "KXLALIGA1HSPREAD"),
    "EPL": ("KXEPLTOTAL", "KXEPLSPREAD", "KXEPL1HTOTAL", "KXEPLTEAMTOTAL"),
    "SERIEA": ("KXSERIEATOTAL", "KXSERIEASPREAD"),
    "BUNDESLIGA": ("KXBUNDESLIGATOTAL", "KXBUNDESLIGASPREAD"),
    "LIGUE1": ("KXLIGUE1TOTAL", "KXLIGUE1SPREAD"),
    "UCL": ("KXUCLTOTAL", "KXUCLSPREAD"),
}

# A book is worth quoting only inside the retail band the edge lives in.
MIN_SPREAD_C, MAX_SPREAD_C = 2, 15
MIN_MID_C, MAX_MID_C = 5, 92


@dataclass(frozen=True)
class Quote:
    """One two-sided book on a makeable market."""

    sub: str
    bid: int
    ask: int
    vol: int

    @property
    def spread(self) -> int:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0

    @property
    def makeable(self) -> bool:
        return (MIN_SPREAD_C <= self.spread <= MAX_SPREAD_C
                and MIN_MID_C <= self.mid <= MAX_MID_C)


Hit = tuple[str, str, str, list[Quote]]


def scan(client: httpx.Client, prefix: str | None) -> list[Hit]:
    """Every (league, series, event) with at least one two-sided quoted SPREAD/TOTAL book."""
    out = []
    for league, series_list in MAKEABLE_SERIES.items():
        for series in series_list:
            if prefix and not series.startswith(prefix):
                continue
            try:
                r = client.get(f"{KALSHI}/events", params={
                    "series_ticker": series, "status": "open",
                    "with_nested_markets": "true", "limit": 40})
                if r.status_code != 200:
                    continue
            except Exception:
                continue
            for e in r.json().get("events", []):
                quoted: list[Quote] = []
                for m in e.get("markets") or []:
                    yb, ya = m.get("yes_bid"), m.get("yes_ask")
                    if yb is None or ya is None:
                        continue
                    quoted.append(Quote(
                        sub=str(m.get("yes_sub_title", "")),
                        bid=int(yb), ask=int(ya), vol=int(m.get("volume") or 0)))
                if quoted:
                    out.append((league, series, str(e.get("title", ""))[:36], quoted))
    return out


def report(hits: list[Hit]) -> bool:
    now = dt.datetime.now(dt.UTC)
    print(f"\n{'=' * 88}\nBIG-FIVE LIVE CHECK — {now:%a %Y-%m-%d %H:%M UTC}\n{'=' * 88}")
    if not hits:
        print("WAIT — no big-five SPREAD/TOTAL book is quoted right now (no game in play).")
        print("  Books populate only once a game is IN PLAY. Typical kickoffs (UTC):")
        print("  EPL Sat 11:30/14:00/16:30 · La Liga 13:00-19:00 · Serie A 13:00-18:45")
        return False
    n_makeable = 0
    for league, series, title, quoted in hits:
        good = [q for q in quoted if q.makeable]
        n_makeable += len(good)
        print(f"\n{league:11s} {title:38s} ({series})  {len(quoted)} quoted, {len(good)} makeable")
        for q in sorted(quoted, key=lambda x: -x.vol)[:6]:
            flag = "  <== MAKEABLE" if q.makeable else ""
            print(f"    {q.sub[:20]:22s} bid {q.bid:>2} ask {q.ask:>2} "
                  f"spr {q.spread:>2}c mid {q.mid:>4.1f} vol {q.vol:>5}{flag}")
    print(f"\n{'-' * 88}")
    if n_makeable:
        print(f"GO — {n_makeable} makeable book(s) live (spread {MIN_SPREAD_C}-{MAX_SPREAD_C}c, "
              f"mid {MIN_MID_C}-{MAX_MID_C}c).")
        print("  Start capture FIRST, then the maker:")
        print("    uv run python -m core.capture.ws_features --prefix KXLALIGA")
        print("    uv run python -m core.maker.lp_live --live --i-understand-live "
              "--pilot KXLALIGA --prefix KXLALIGA --minutes 60")
    else:
        print("PARTIAL — quoted, but none in the makeable band "
              f"(spr {MIN_SPREAD_C}-{MAX_SPREAD_C}c, mid {MIN_MID_C}-{MAX_MID_C}c). Keep watching.")
    return bool(n_makeable)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", default=None, help="restrict to series with this prefix")
    ap.add_argument("--watch", action="store_true", help="poll until a makeable book appears")
    ap.add_argument("--every", type=int, default=120, help="watch poll interval (s)")
    args = ap.parse_args()
    with httpx.Client(timeout=20, headers={"User-Agent": "crypto-de/live-check"}) as c:
        while True:
            if report(scan(c, args.prefix)) or not args.watch:
                return 0
            time.sleep(args.every)


if __name__ == "__main__":
    raise SystemExit(main())
