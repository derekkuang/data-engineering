"""Soccer fixture calendar from Kalshi itself — when is the next makeable window? ($0)

Fixed-cron scheduling has two failure modes we measured: GitHub's `schedule` fires
1h47m-4h39m LATE (2026-09-08), and even a punctual cron wastes windows on days with no
match while missing kickoffs on days that have several. Both are symptoms of scheduling by
CLOCK instead of by FIXTURE.

Kalshi carries the kickoff itself: each market exposes ``occurrence_datetime`` (and
``expected_expiration_time``), so the fixture list can be derived from the same API we
already trade on — no third-party sports feed, no key, no scraping. (ESPN's scoreboard
403s from outside the US anyway.)

Used to answer two operational questions:
  * "when should I be at the keyboard?"  -> `upcoming` / the CLI table
  * "is a window open right now?"        -> `windows_now`, for a scheduler to fire on

NOTE this lists SCHEDULED fixtures. It does NOT mean a book is makeable — that needs the
live gate (spread band + per-ticker activity), which `soccer_mm.live_check` applies. A
fixture is a necessary condition, not a sufficient one.

Usage::

    uv run python -m strategies.soccer_mm.fixtures              # next 24h
    uv run python -m strategies.soccer_mm.fixtures --hours 72
    uv run python -m strategies.soccer_mm.fixtures --now        # windows open right now
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass

import httpx

KALSHI = "https://external-api.kalshi.com/trade-api/v2"

# The makeable series (TOTAL/SPREAD). KX<LEAGUE>GAME is the 3-way match result — directional,
# never quoted by the maker — so it is deliberately absent.
SERIES = [
    f"KX{lg}{suf}"
    for lg in ("EPL", "LALIGA", "SERIEA", "BUNDESLIGA", "LIGUE1", "UCL", "UEL",
               "MLS", "LIGAMX", "BRASILEIRO", "EREDIVISIE", "EFLCHAMPIONSHIP")
    for suf in ("TOTAL", "SPREAD")
]

# A match is tradeable from kickoff until roughly full time + stoppage.
MATCH_MINUTES = 115.0


@dataclass(frozen=True)
class Fixture:
    league: str
    event: str
    kickoff: dt.datetime

    @property
    def ends(self) -> dt.datetime:
        return self.kickoff + dt.timedelta(minutes=MATCH_MINUTES)

    def live_at(self, when: dt.datetime) -> bool:
        return self.kickoff <= when <= self.ends


def _parse(raw: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def fetch(client: httpx.Client, hours: float = 24.0) -> list[Fixture]:
    """Scheduled soccer fixtures within `hours`, deduped per (league, match)."""
    now = dt.datetime.now(dt.UTC)
    horizon = now + dt.timedelta(hours=hours)
    seen: dict[tuple[str, str], Fixture] = {}
    for series in SERIES:
        try:
            r = client.get(f"{KALSHI}/events", params={
                "series_ticker": series, "status": "open",
                "with_nested_markets": "true", "limit": 60})
            if r.status_code != 200:
                continue
        except Exception:
            continue
        league = series.replace("KX", "").replace("TOTAL", "").replace("SPREAD", "")
        for e in r.json().get("events", []):
            ko = None
            for m in e.get("markets") or []:
                ko = _parse(str(m.get("occurrence_datetime") or ""))
                if ko:
                    break
            if ko is None:
                continue
            ev = str(e.get("event_ticker", ""))
            # ticker tail identifies the match (…-26SEP18ARSMCI-…); key on it so TOTAL and
            # SPREAD of the same fixture collapse to one row.
            match = ev.split("-")[1] if "-" in ev else ev
            if now - dt.timedelta(minutes=MATCH_MINUTES) <= ko <= horizon:
                seen.setdefault((league, match), Fixture(league, ev, ko))
    return sorted(seen.values(), key=lambda f: f.kickoff)


def windows_now(fixtures: list[Fixture], when: dt.datetime | None = None) -> list[Fixture]:
    """Fixtures that should be IN PLAY at `when` — i.e. a scheduler should be running."""
    t = when or dt.datetime.now(dt.UTC)
    return [f for f in fixtures if f.live_at(t)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--now", action="store_true", help="only fixtures live right now")
    args = ap.parse_args()
    now = dt.datetime.now(dt.UTC)
    with httpx.Client(timeout=20, headers={"User-Agent": "crypto-de/fixtures"}) as c:
        fx = fetch(c, args.hours)
    if args.now:
        fx = windows_now(fx, now)
        if not fx:
            print(f"No fixture in play at {now:%a %H:%M UTC}.")
            return 1
    if not fx:
        print(f"No soccer fixtures in the next {args.hours:.0f}h.")
        return 1
    print(f"now {now:%a %Y-%m-%d %H:%M UTC}   |   {len(fx)} fixture(s)\n")
    print(f"{'kickoff (UTC)':18s}{'in':>9}{'league':14s}  match")
    print("-" * 68)
    for f in fx:
        delta = (f.kickoff - now).total_seconds() / 3600.0
        when = "LIVE" if f.live_at(now) else f"{delta:+.1f}h"
        match = f.event.split("-")[1] if "-" in f.event else f.event
        print(f"{f.kickoff:%a %m-%d %H:%M}  {when:>9}  {f.league:14s}{match}")
    live = windows_now(fx, now)
    print(f"\n{len(live)} fixture(s) IN PLAY now. A fixture is NECESSARY but not SUFFICIENT — "
          f"\nrun `soccer_mm.live_check` for the makeable gate (spread band + activity).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
