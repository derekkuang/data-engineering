"""Does the thin prediction market (Kalshi) diverge PROFITABLY from the sharp book (DraftKings)?

Candidate edge #3 from the 2026-07-29 edge scan. The one fresh NON-market-making angle: a
sharp sportsbook's closing line is the best public forecast of a game; when a thinner venue
(Kalshi) prices the same game away from it by more than fees, betting toward the sharp line
should have positive EV. Crucially this is NOT the dead cross-venue crypto arb — that died on
SETTLEMENT BASIS (Kalshi's BRTI TWAP vs Polymarket's Chainlink point-read disagree ~5% of the
time). A game moneyline settles on the UNAMBIGUOUS result, identical on both venues, so there
is no basis risk; you are borrowing DraftKings' model, not racing a second index.

This is a real HISTORICAL backtest on 100%-free data (no paid odds feed):
  * ESPN's keyless endpoints retain, per past date, each completed game's CLOSING DraftKings
    moneyline + total AND the winner (site.api.espn.com .../scoreboard?dates= + .../summary).
  * Kalshi settled game markets (KXMLBGAME) give the pre-game price via candlesticks + the
    result. The ticker encodes date + both team codes, so games match by (ET date, team set).

Method (per matched game):
  * Devig DraftKings' two-way moneyline -> fair_prob (remove the ~4-5% hold).
  * Read Kalshi's pre-game book (mid + ask) at the game's start time from candlesticks.
  * For each side, EV of BUYING it on Kalshi = dk_fair - kalshi_ask - taker_fee. Bet the
    best-EV side per game when EV clears a threshold; realized P&L = (win? 1 : 0) - ask - fee.
  * Discipline (the project's own): resample by DAY (day-block bootstrap CI), split-half sign,
    and report the divergence DISTRIBUTION + whether DraftKings' line is even sharper than
    Kalshi's (log loss) — if Kalshi is already as sharp, there is nothing to capture.

Costs: Kalshi TAKER fee ~= ceil(7*p*(1-p)) cents/contract (~1.75c at 50/50), paid on entry;
you buy at the ask (the spread is already in `ask`). MLB moneyline is a taker strategy (buy
to hold to settlement), so no maker rebate applies.

Caveats: ESPN's line is DraftKings single-book (not a multi-book sharp consensus) and is the
CLOSING snapshot (not necessarily the exact time of the Kalshi read); Kalshi's pre-game book
can be thin. This is a first honest cut, not an execution model.

Usage:
    uv run python -m strategies.cross_venue.sportsbook_divergence --days 14
    uv run python -m strategies.cross_venue.sportsbook_divergence --days 21 --edge 0.03
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np
from dotenv import load_dotenv
from sklearn.metrics import log_loss

from core.maker.edge_verdict import day_block_ci  # the project's canonical day-block bootstrap CI
from ingestion.kalshi import KalshiClient
from ingestion.kalshi import kalshi_taker_fee as taker_fee

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ET = "America/New_York"
EDGE_THRESHOLDS = (0.0, 0.01, 0.02, 0.03, 0.05)

# Canonical team codes: fold the ESPN and Kalshi spelling variants onto one code so a game
# matches by (date, frozenset of two teams) regardless of which venue's abbreviation it came in.
TEAM_ALIAS = {
    "CWS": "CHW", "CHA": "CHW",           # White Sox
    "AZ": "ARI", "ARZ": "ARI",            # Diamondbacks
    "OAK": "ATH",                          # Athletics
    "WAS": "WSH", "WSN": "WSH",           # Nationals
    "SDP": "SD", "SFG": "SF", "TBR": "TB", "KCR": "KC", "CHN": "CHC",
    # NOTE: no bare "LA" alias — it would merge the Angels (LAA) into the Dodgers (LAD); both
    # ESPN and Kalshi already use the distinct LAA/LAD codes.
}


def _canon(code: str) -> str:
    c = code.upper()
    return TEAM_ALIAS.get(c, c)


# --------------------------------------------------------------------------- odds math
def american_to_prob(ml: float) -> float:
    """American moneyline -> implied win probability (still carries the book's vig)."""
    return -ml / (-ml + 100.0) if ml < 0 else 100.0 / (ml + 100.0)


def devig_two_way(home_ml: float, away_ml: float) -> tuple[float, float]:
    """Two-way moneyline -> vig-free fair probs (normalize so they sum to 1)."""
    qh, qa = american_to_prob(home_ml), american_to_prob(away_ml)
    s = qh + qa
    return qh / s, qa / s


# --------------------------------------------------------------------------- ESPN side
@dataclass
class EspnGame:
    et_date: str            # YYYY-MM-DD (ET) — the slate day, matches the Kalshi ticker date
    start_ts: int           # UTC unix seconds (authoritative game start)
    home: str               # canonical code
    away: str
    dk_fair_home: float     # devigged DraftKings fair prob that HOME wins
    winner: str | None      # canonical code of the winner (None if not decided/parsed)

    @property
    def key(self) -> frozenset[str]:
        return frozenset({self.home, self.away})


def _espn_scoreboard(http: httpx.Client, sport_path: str, yyyymmdd: str) -> list[dict[str, Any]]:
    r = http.get(f"{ESPN_BASE}/{sport_path}/scoreboard", params={"dates": yyyymmdd})
    r.raise_for_status()
    return list(r.json().get("events", []))


def _espn_dk_line(http: httpx.Client, sport_path: str, event_id: str) -> dict[str, Any] | None:
    """DraftKings pickcenter line for one event (has home/away moneyline)."""
    r = http.get(f"{ESPN_BASE}/{sport_path}/summary", params={"event": event_id})
    r.raise_for_status()
    pc = r.json().get("pickcenter") or []
    dk = [o for o in pc if o.get("provider", {}).get("name") == "DraftKings"]
    return dk[0] if dk else None  # DraftKings only — never silently substitute another book


def espn_games(http: httpx.Client, sport_path: str, yyyymmdd: str, et_date: str) -> list[EspnGame]:
    """Completed games on a date with a DraftKings two-way moneyline + the winner."""
    out: list[EspnGame] = []
    for e in _espn_scoreboard(http, sport_path, yyyymmdd):
        status = (e.get("status") or {}).get("type") or {}
        if not status.get("completed"):
            continue
        comp = (e.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_code = _canon((home.get("team") or {}).get("abbreviation", ""))
        away_code = _canon((away.get("team") or {}).get("abbreviation", ""))
        winner = None
        if home.get("winner"):
            winner = home_code
        elif away.get("winner"):
            winner = away_code
        line = _espn_dk_line(http, sport_path, e["id"])
        if not line:
            continue
        hml = (line.get("homeTeamOdds") or {}).get("moneyLine")
        aml = (line.get("awayTeamOdds") or {}).get("moneyLine")
        # American odds are always |ml| >= 100; a 0 / sub-100 value is an unset placeholder line
        # (american_to_prob(0) would fabricate a 1.0 fair prob) -> reject it, not just None.
        if hml is None or aml is None or abs(float(hml)) < 100 or abs(float(aml)) < 100:
            continue
        fair_home, _ = devig_two_way(float(hml), float(aml))
        start_ts = int(dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp())
        out.append(EspnGame(et_date, start_ts, home_code, away_code, fair_home, winner))
    return out


# --------------------------------------------------------------------------- Kalshi side
@dataclass
class KalshiSide:
    market_ticker: str
    team: str               # canonical code (this market = "team wins")
    result_yes: bool        # did this side win (market result == 'yes')


@dataclass
class KalshiGame:
    event_ticker: str
    et_date: str            # from the ticker (YYMONDD, ET)
    sides: list[KalshiSide]

    @property
    def key(self) -> frozenset[str]:
        return frozenset(_canon(s.team) for s in self.sides)


_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def _parse_ticker_date(event_ticker: str) -> str | None:
    """KXMLBGAME-26JUL281940NYYCWS -> '2026-07-28' (ET slate date encoded in the ticker)."""
    parts = event_ticker.split("-")
    if len(parts) < 2 or len(parts[1]) < 7:
        return None
    tok = parts[1]  # e.g. 26JUL281940NYYCWS
    try:
        yy, mon, dd = int(tok[0:2]), tok[2:5], int(tok[5:7])
        return f"20{yy:02d}-{_MONTHS[mon]:02d}-{dd:02d}"
    except (ValueError, KeyError):
        return None


def kalshi_settled_games(
    client: KalshiClient, series: str, days: int
) -> tuple[dict[tuple[str, frozenset[str]], KalshiGame], int]:
    """Settled game events over the window, keyed by (ET date, the two team codes) — NOT by team
    set alone: a 3-4 game series (same matchup on consecutive dates) would otherwise collapse to
    a single surviving game. Same-date/same-teams DOUBLEHEADERS are ambiguous (which game's
    outcome pairs with which price?), so both are dropped and counted. Each side market's
    `result` gives the winner; the ticker suffix gives the side's team code.
    Returns (games keyed by (date, teams), n_doubleheader_games_dropped)."""
    now = dt.datetime.now(dt.UTC)
    start = now - dt.timedelta(days=days)
    mkts = client.list_markets(
        series, status="settled",
        min_close_ts=int(start.timestamp()), max_close_ts=int(now.timestamp()),
    )
    by_ev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in mkts:
        by_ev[m.get("event_ticker", "")].append(m)

    games: dict[tuple[str, frozenset[str]], KalshiGame] = {}
    dropped: set[tuple[str, frozenset[str]]] = set()  # (date,teams) seen more than once = DH
    dropped_dh = 0
    for ev, ms in by_ev.items():
        et_date = _parse_ticker_date(ev)
        if et_date is None or len(ms) != 2:  # malformed / not a two-sided game market
            continue
        sides = [
            KalshiSide(m["ticker"], _canon(m["ticker"].split("-")[-1]), m.get("result") == "yes")
            for m in ms
        ]
        g = KalshiGame(ev, et_date, sides)
        k = (et_date, g.key)
        if k in dropped:            # a 3rd+ collision on an already-dropped DH key
            dropped_dh += 1
            continue
        if k in games:              # doubleheader: same teams same day -> drop BOTH, ambiguous
            del games[k]
            dropped.add(k)
            dropped_dh += 2
            continue
        games[k] = g
    return games, dropped_dh


def kalshi_pregame_books(
    client: KalshiClient, tickers: list[str], start_ts: int
) -> dict[str, tuple[float, float]]:
    """(mid, ask) per ticker at ~game start — both sides of a game batched into ONE candlestick
    call (they share the window). For each market the LATEST candle whose period ends at/before
    start_ts (strictly pre-game, no in-game look-ahead) with a two-sided book wins. Robust to
    candle ordering (selects by max end_period_ts, not list position). Missing = thin market."""
    by_ticker = client.get_market_candlesticks_batch(
        tickers, start_ts - 120 * 60, start_ts + 5 * 60, period_interval=1
    )
    out: dict[str, tuple[float, float]] = {}
    for ticker, cs in by_ticker.items():
        best: tuple[float, float] | None = None
        best_ts = -1
        for c in cs:
            ts = int(c.get("end_period_ts", 0))
            if ts > start_ts:  # strictly pre-game only
                continue
            yb = (c.get("yes_bid") or {}).get("close_dollars")
            ya = (c.get("yes_ask") or {}).get("close_dollars")
            if yb is None or ya is None:
                continue
            yb_f, ya_f = float(yb), float(ya)
            if 0.0 < yb_f < ya_f < 1.0 and ts > best_ts:
                best, best_ts = ((yb_f + ya_f) / 2.0, ya_f), ts
        if best is not None:
            out[ticker] = best
    return out


# --------------------------------------------------------------------------- backtest
@dataclass
class Bet:
    et_date: str
    game: str
    team: str
    dk_fair: float          # DraftKings fair prob this side wins
    kalshi_mid: float       # Kalshi implied prob this side wins (pre-game)
    ask: float              # Kalshi ask you'd pay to buy this side
    won: bool
    edge: float             # dk_fair - ask - fee (expected value per contract)
    pnl: float              # realized: (1 if won else 0) - ask - fee


def build_bets(espn_by_key: dict[str, dict[frozenset[str], EspnGame]],
               kalshi: dict[tuple[str, frozenset[str]], KalshiGame],
               client: KalshiClient) -> tuple[list[Bet], dict[str, int]]:
    """Match ESPN<->Kalshi games by (date, teams), read Kalshi's pre-game book for BOTH sides
    (one batched candlestick call per game), and form the best-EV candidate bet per game.
    Returns (candidate bets, match diagnostics)."""
    diag = {"espn_games": 0, "matched": 0, "no_book": 0, "no_winner": 0}
    espn_idx: dict[tuple[str, frozenset[str]], EspnGame] = {}
    for date, by_key in espn_by_key.items():
        for key, g in by_key.items():
            espn_idx[(date, key)] = g
            diag["espn_games"] += 1

    bets: list[Bet] = []
    for (date, key), eg in espn_idx.items():
        kg = kalshi.get((date, key))
        if kg is None:
            continue
        if eg.winner is None:
            diag["no_winner"] += 1
            continue
        diag["matched"] += 1
        books = kalshi_pregame_books(client, [s.market_ticker for s in kg.sides], eg.start_ts)
        cands: list[Bet] = []
        for s in kg.sides:
            book = books.get(s.market_ticker)
            if book is None:
                continue
            mid, ask = book
            dk_fair = eg.dk_fair_home if s.team == eg.home else 1.0 - eg.dk_fair_home
            fee = taker_fee(ask)
            won = (eg.winner == s.team)
            cands.append(Bet(date, f"{eg.away}@{eg.home}", s.team, dk_fair, mid, ask, won,
                             dk_fair - ask - fee, (1.0 if won else 0.0) - ask - fee))
        if not cands:
            diag["no_book"] += 1
            continue
        bets.append(max(cands, key=lambda b: b.edge))
    return bets, diag


def report(bets: list[Bet], diag: dict[str, int], edge_min: float) -> None:
    print("=" * 90)
    print("SPORTSBOOK-DIVERGENCE BACKTEST — fade Kalshi toward the DraftKings fair line")
    print("=" * 90)
    print(f"ESPN completed games: {diag['espn_games']}   matched to Kalshi: {diag['matched']}   "
          f"priceable: {len(bets)}   (dropped: {diag['no_winner']} no-winner, "
          f"{diag['no_book']} no pre-game book, {diag.get('dh_dropped', 0)} doubleheader)")
    if not bets:
        print("\nNo priceable matched games — widen --days or check the season is live.")
        return

    rng = np.random.default_rng(7)
    b = np.array([1.0 if x.won else 0.0 for x in bets])
    dk = np.array([x.dk_fair for x in bets])
    ks = np.array([x.kalshi_mid for x in bets])
    div = ks - dk  # Kalshi minus DraftKings-fair (signed divergence on the chosen side)

    # Is DraftKings even sharper than Kalshi? (if equal, there's nothing to borrow)
    dk_ll = log_loss(b, dk, labels=[0, 1])
    ks_ll = log_loss(b, ks, labels=[0, 1])
    print(f"\nSHARPNESS on {len(bets)} matched games (log loss vs outcome; lower = sharper):")
    print(f"  DraftKings devig {dk_ll:.4f}   Kalshi pre-game mid {ks_ll:.4f}")
    print(f"  |divergence| Kalshi-vs-DK: median {np.median(np.abs(div)) * 100:.2f}c   "
          f"p90 {np.percentile(np.abs(div), 90) * 100:.2f}c   "
          f"(fee at 50/50 ~= {taker_fee(0.5) * 100:.0f}c)")

    print("\nSTRATEGY — buy the best-EV side per game when EV (dk_fair - ask - fee) > threshold:")
    print(f"  {'thr':>5}{'bets':>6}{'bet%':>7}{'hit%':>7}{'avg_edge':>10}"
          f"{'pnl/ct($)':>11}{'ROI':>9}{'day-block 95% CI ($/ct)':>26}")
    for thr in EDGE_THRESHOLDS:
        sel = [x for x in bets if x.edge > thr]
        if not sel:
            print(f"  {thr:>5.2f}{0:>6}")
            continue
        pnl = np.array([x.pnl for x in sel])
        cost = np.array([x.ask + taker_fee(x.ask) for x in sel])
        by_day: dict[str, list[float]] = defaultdict(list)
        for x in sel:
            by_day[x.et_date].append(x.pnl)
        day_means = np.array([float(np.mean(v)) for v in by_day.values()])
        point, lo, hi = day_block_ci(day_means, rng)
        roi = pnl.sum() / cost.sum() if cost.sum() else 0.0
        print(f"  {thr:>5.2f}{len(sel):>6}{len(sel) / len(bets):>7.0%}"
              f"{np.mean([x.won for x in sel]):>7.0%}{np.mean([x.edge for x in sel]) * 100:>9.2f}c"
              f"{pnl.mean():>+11.4f}{roi:>+9.2%}   [{lo:+.4f},{hi:+.4f}] ({len(day_means)}d)")

    # split-half stability at the chosen threshold
    sel = sorted([x for x in bets if x.edge > edge_min], key=lambda x: x.et_date)
    if len(sel) >= 8:
        half = len(sel) // 2
        a = float(np.mean([x.pnl for x in sel[:half]]))
        c = float(np.mean([x.pnl for x in sel[half:]]))
        agree = "AGREE" if np.sign(a) == np.sign(c) else "DISAGREE"
        print(f"\nsplit-half pnl/ct at thr={edge_min}: {a:+.4f} vs {c:+.4f} -> {agree}")

    print("\nRead: a positive, CI-entirely-above-0 pnl/ct that SURVIVES the fee and is split-half"
          " stable = a real edge. DK log loss << Kalshi = there IS something to borrow;"
          " DK ~= Kalshi = the venues agree and any 'divergence' is noise inside spread + fee.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi-vs-DraftKings divergence backtest (sports)")
    ap.add_argument("--days", type=int, default=14, help="days of settled history to backtest")
    ap.add_argument("--series", default="KXMLBGAME", help="Kalshi game-moneyline series")
    ap.add_argument("--sport-path", default="baseball/mlb", help="ESPN sport path")
    ap.add_argument("--edge", type=float, default=0.02, help="edge threshold for split-half")
    args = ap.parse_args()

    load_dotenv()
    http = httpx.Client(timeout=25, headers={"User-Agent": "Mozilla/5.0 (research)"})
    client = KalshiClient(pace_seconds=0.15)
    try:
        # Kalshi settled games over the window (one list call).
        kalshi, kalshi_dh = kalshi_settled_games(client, args.series, args.days)
        # ESPN games per ET date over the same window.
        espn_by_key: dict[str, dict[frozenset[str], EspnGame]] = {}
        today_et = dt.datetime.now(ZoneInfo(ET)).date()  # real ET (DST-correct), not a fixed -4
        for d in range(1, args.days + 1):
            day = today_et - dt.timedelta(days=d)
            yyyymmdd = day.strftime("%Y%m%d")
            et_date = day.strftime("%Y-%m-%d")
            try:
                games = espn_games(http, args.sport_path, yyyymmdd, et_date)
            except httpx.HTTPError:
                continue
            # drop doubleheaders (same teams same day) — ambiguous which game a price pairs with
            by_key: dict[frozenset[str], EspnGame] = {}
            dupes: set[frozenset[str]] = set()
            for g in games:
                if g.key in by_key:
                    dupes.add(g.key)
                by_key[g.key] = g
            espn_by_key[et_date] = {k: v for k, v in by_key.items() if k not in dupes}
        print(f"Kalshi settled games: {len(kalshi)}   ESPN dates fetched: {len(espn_by_key)}")
        bets, diag = build_bets(espn_by_key, kalshi, client)
        diag["dh_dropped"] = kalshi_dh
    finally:
        http.close()
        client.close()

    report(bets, diag, args.edge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
