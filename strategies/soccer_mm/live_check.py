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

from core.maker.lp_gate import MIN_RECENT_TRADES, passes_gate
from core.maker.lp_pilot import is_mean_reverting

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
    ticker: str
    bid: int
    ask: int
    vol: int
    recent_trades: int

    @property
    def spread(self) -> int:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2.0

    @property
    def in_band(self) -> bool:
        """Spread/mid inside the retail band — necessary but NOT sufficient."""
        return (MIN_SPREAD_C <= self.spread <= MAX_SPREAD_C
                and MIN_MID_C <= self.mid <= MAX_MID_C)

    @property
    def makeable(self) -> bool:
        """What the BOT would actually quote: in-band AND mean-reverting AND past the
        recent-trade floor. Without the activity floor a pre-game token quote (2-6c spread,
        ZERO volume) looks 'makeable' while the bot correctly idles on it — wide != rich."""
        return (self.in_band and is_mean_reverting(self.ticker)
                and passes_gate(self.ticker, self.recent_trades))


Hit = tuple[str, str, str, list[Quote]]


def recent_trade_counts(client: httpx.Client) -> dict[str, int]:
    """Recent trades per ticker from the public tape — the SAME activity signal
    pick_smooth_ticker gates on (a book with no recent prints cannot be made in)."""
    try:
        r = client.get(f"{KALSHI}/markets/trades", params={"limit": 1000})
        r.raise_for_status()
        counts: dict[str, int] = {}
        for t in r.json().get("trades", []):
            tk = str(t.get("ticker", ""))
            if tk:
                counts[tk] = counts.get(tk, 0) + 1
        return counts
    except Exception:
        return {}


def scan(client: httpx.Client, prefix: str | None) -> list[Hit]:
    """Every (league, series, event) with at least one two-sided quoted SPREAD/TOTAL book."""
    out = []
    trades = recent_trade_counts(client)
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
                    # Kalshi returns prices as DOLLAR STRINGS (yes_bid_dollars "0.9500"),
                    # not the legacy integer-cent `yes_bid`. Reading the old names silently
                    # yields None -> every book looks unquoted (a false "no game in play").
                    yb, ya = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
                    if yb is None or ya is None:
                        continue
                    bid_c, ask_c = round(float(yb) * 100), round(float(ya) * 100)
                    if bid_c <= 0 or ask_c <= 0:
                        continue
                    tk = str(m["ticker"])
                    quoted.append(Quote(
                        sub=str(m.get("yes_sub_title", "")), ticker=tk,
                        bid=bid_c, ask=ask_c,
                        vol=int(float(m.get("volume_24h_fp") or 0)),
                        recent_trades=trades.get(tk, 0)))
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
            flag = "  <== BOT-QUOTABLE" if q.makeable else (
                "  (in band, but no recent trades)" if q.in_band else "")
            print(f"    {q.sub[:20]:22s} bid {q.bid:>2} ask {q.ask:>2} "
                  f"spr {q.spread:>2}c mid {q.mid:>4.1f} vol {q.vol:>5} "
                  f"trades {q.recent_trades:>3}{flag}")
    print(f"\n{'-' * 88}")
    if n_makeable:
        print(f"GO — {n_makeable} book(s) the bot would actually quote "
              f"(in band AND >={MIN_RECENT_TRADES} recent trades).")
        print("  Start capture FIRST, then the maker:")
        print("    uv run python -m core.capture.ws_features --prefix KXLALIGA")
        print("    uv run python -m core.maker.lp_live --live --i-understand-live "
              "--pilot KXLALIGA --prefix KXLALIGA --minutes 60")
    else:
        n_band = sum(1 for _, _, _, qs in hits for q in qs if q.in_band)
        print(f"WAIT — books are quoted and {n_band} sit in the spread/mid band, but NONE pass "
              f"the recent-trade floor (>={MIN_RECENT_TRADES}). Pre-game books are either "
              "competed to 1c or untraded token quotes; the bot idles on both (wide != rich). "
              "Real makeable spread appears once a game is IN PLAY.")
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
