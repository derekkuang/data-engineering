"""LP pilot — PHASE A: paper market-making against the LIVE Kalshi book.

This is the ground truth Stage 2 couldn't get from historical minute candles: it
quotes a real maker strategy against the live order book in real time and measures
YOUR realized fills + markout at SECONDS resolution. No money at risk (fills are
simulated); it is the exact skeleton of the live bot (Phase B just swaps the
simulated fill for a real order).

Strategy = JOIN THE TOUCH. Each poll we read the live book and "rest" a 1-contract
maker bid at best_bid and ask at best_ask. A printed trade fills us when:
  * it prints at/below our bid  -> we BUY 1 at our bid (a taker sold into us), or
  * it prints at/above our ask  -> we SELL 1 at our ask (a taker lifted us).

Two honest caveats:
  * FILL RATE IS AN UPPER BOUND. We assume a trade at our price fills us, i.e. we
    ignore queue position (unknowable on paper). The true fill rate only comes
    from Phase B (real resting orders). So treat fill counts as optimistic.
  * MARKOUT IS TRUSTWORTHY. Whether the mid moves against us after a fill does NOT
    depend on queue position, so the markout — the toxicity signal — is real, and
    measured here at seconds resolution (finer than Stage 2's 1-min candles).

Per fill, marked at horizon h:  pnl(h) = side * (mid[t+h] - fill_price)
  = edge (≈ half-spread captured) + markout (the adverse move). We report the
total, the isolated markout, the (upper-bound) fill rate, and the inventory path.

Picks the most actively-trading BENIGN market right now (sports/entertainment)
from the live trade feed, or pass --ticker.

Usage:
    uv run python -m core.maker.lp_paper_pilot --minutes 20
    uv run python -m core.maker.lp_paper_pilot --ticker KXMLBGAME-26JUN15... --minutes 30
    # quote 10 political favorites at once (pools fills -> markout accumulates ~10x faster):
    uv run python -m core.maker.lp_paper_pilot --category Politics,Elections,World,Economics \
        --markets 10 --poll 6 --minutes 25
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from core.maker.lp_pilot import JUMPY, enumerate_candidates, is_mean_reverting
from ingestion.kalshi import KalshiClient

POLL_SECONDS = 4.0
MARKOUT_HORIZONS = (15, 30, 60)  # seconds
# Benign (retail/slow) series prefixes; EXCLUDE fast/efficient crypto + 15m/perp.
ELIGIBLE_PREFIXES = (
    "KXMLB",
    "KXNCAA",
    "KXWNBA",
    "KXATP",
    "KXITF",
    "KXWC",
    "KXPGA",
    "KXUFC",
    "KXNFL",
    "KXNBA",
    "KXNHL",
    "KXSOCCER",
    "KXRT",
    "KXTENNIS",
    "KXBOXING",
    "KXGOLF",
    # Club soccer — the post-WC primary hypothesis. This list had DIVERGED from
    # lp_pilot.ELIGIBLE_PREFIXES and carried no club-soccer prefixes, so a default-universe
    # run could never find the very markets the hypothesis is about (only an explicit
    # --prefix worked). Kept in sync with lp_pilot deliberately.
    "KXMLS",
    "KXLIGAMX",
    "KXBRASILEIRO",
    "KXEPL",
    "KXLALIGA",
    "KXSERIEA",
    "KXBUNDESLIGA",
    "KXLIGUE1",
    "KXUCL",
    "KXUEL",
    "KXEREDIVISIE",
    "KXEFLCHAMPIONSHIP",
)
EXCLUDE = ("15M", "PERP", "KXBTC", "KXETH", "KXSOL", "KXHYPE", "KXXRP", "KXDOGE")


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


@dataclass
class Fill:
    ts: float
    side: int  # +1 we bought (long), -1 we sold (short)
    price: float
    mid_at_fill: float


@dataclass
class Pilot:
    ticker: str
    mids: list[tuple[float, float]] = field(default_factory=list)  # (ts, mid)
    fills: list[Fill] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    n_polls: int = 0
    spread_sum: float = 0.0


def best_bid_ask(book: dict[str, Any]) -> tuple[float, float] | None:
    """(best_yes_bid, best_yes_ask) in dollars. yes_dollars = yes bids; no_dollars
    = no bids, and a no bid at p == a yes ask at 1-p, so best_yes_ask = 1-best_no_bid."""
    yes = book.get("yes_dollars") or book.get("yes") or []
    no = book.get("no_dollars") or book.get("no") or []
    yb = [float(p) for p, _ in yes] if yes else []
    nb = [float(p) for p, _ in no] if no else []
    if not yb or not nb:
        return None
    return max(yb), round(1.0 - max(nb), 4)


def pick_benign_tickers(client: KalshiClient, prefixes: tuple[str, ...] | None = None,
                        series_set: set[str] | None = None, n: int = 1,
                        max_per_event: int = 2) -> list[str]:
    """Up to ``n`` most actively-trading MAKEABLE benign markets right now, from the live trade
    feed. ``prefixes`` restricts the universe (e.g. club-soccer prefixes); ``series_set`` (a set of
    series tickers, e.g. all political series from list_series-by-category) OVERRIDES the prefix
    match — politics has thousands of heterogeneous series, so match by series membership, not a
    prefix list. Default = the broad benign board. Quoting several favorites at once is how the
    slow politics markout accumulates in days rather than weeks (fills pool across markets)."""
    pfx = prefixes or ELIGIBLE_PREFIXES
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    counts: Counter[str] = Counter()
    for t in trades:
        tk = t.get("ticker", "")
        if any(x in tk for x in EXCLUDE):
            continue
        matched = (tk.split("-")[0] in series_set) if series_set is not None \
            else any(tk.startswith(p) for p in pfx)
        # The tape path historically applied only EXCLUDE (crypto) + a spread band, so it
        # admitted market types the maker must NEVER quote: BTTS/GOAL/CARD discrete-jump props
        # and the directional GAME / 1H-winner books. (Measured 2026-09-05: 4 of 10 picks were
        # BTTS or GAME.) Apply the same allowlist enumerate_candidates uses. Politics
        # (series_set) is exempt — it has no TOTAL/SPREAD analogue.
        if matched and series_set is None:
            if not is_mean_reverting(tk) or any(j in tk for j in JUMPY):
                matched = False
        if matched:
            counts[tk] += 1
    # The shared tape is a fixed 1000-row exchange-wide window, so a high-frequency series
    # crowds soccer out of it (KXBTC15M held 322/1000; a 138-print/5min book read as ~2).
    # Add the tape-INDEPENDENT per-series enumeration so those books are visible at all.
    ordered = [tk for tk, _ in counts.most_common(max(25, 4 * n))]
    if series_set is None:
        seen = set(ordered)
        ordered += [tk for tk in enumerate_candidates(client, pfx) if tk not in seen]
    picked: list[str] = []
    per_event: Counter[str] = Counter()
    for tk in ordered:
        # DIVERSIFY ACROSS GAMES. The buckets of one match (Over 1.5/2.5/3.5...) are the SAME
        # bet — a goal moves all of them together — so filling N slots from one game gives N x
        # the exposure, not N independent samples. That is exactly how a pooled read gets
        # dominated by a single directional episode. Key on the match segment of the ticker
        # (KXEPLTOTAL-26SEP05MCICOV-4 -> 26SEP05MCICOV) so TOTAL and SPREAD of the same match
        # share a budget.
        parts = tk.split("-")
        event = parts[1] if len(parts) > 1 else tk
        if per_event[event] >= max_per_event:
            continue
        book = client.get_market_orderbook(tk)
        ba = best_bid_ask(book)
        if ba and 0.02 <= (ba[1] - ba[0]) <= 0.15:  # 2-15c spread = makeable, not broken
            picked.append(tk)
            per_event[event] += 1
            if len(picked) >= n:
                break
    return picked


def pick_benign_ticker(client: KalshiClient, prefixes: tuple[str, ...] | None = None,
                       series_set: set[str] | None = None) -> str | None:
    """Single most-active makeable benign market (n=1 wrapper over pick_benign_tickers)."""
    picked = pick_benign_tickers(client, prefixes, series_set, n=1)
    return picked[0] if picked else None


def poll_once(client: KalshiClient, p: Pilot) -> None:
    now = time.time()
    book = client.get_market_orderbook(p.ticker)
    ba = best_bid_ask(book)
    if ba is None:
        return
    bid, ask = ba
    mid = (bid + ask) / 2.0
    p.mids.append((now, mid))
    p.n_polls += 1
    p.spread_sum += ask - bid

    # New trades since last poll -> simulate fills against our resting touch quotes.
    trades = client.get("/markets/trades", params={"ticker": p.ticker, "limit": 100}).get(
        "trades", []
    )
    for t in trades:
        tid = t.get("trade_id")
        if not tid or tid in p.seen:
            continue
        p.seen.add(tid)
        px = _f(t.get("yes_price_dollars"))
        if px is None:
            continue
        if px <= bid:  # taker sold into our bid -> we buy
            p.fills.append(Fill(now, +1, bid, mid))
        elif px >= ask:  # taker lifted our ask -> we sell
            p.fills.append(Fill(now, -1, ask, mid))


def _mid_at(p: Pilot, ts: float) -> float | None:
    """First recorded mid at or after ts (None if the run ended first)."""
    for t, m in p.mids:
        if t >= ts:
            return m
    return None


def markout_rows(pilots: list[Pilot]) -> list[tuple[int, int, float, float]]:
    """Pooled markout per horizon: (horizon_s, n_marked_fills, mean_markout_c, mean_net_c). Each
    fill is marked against ITS OWN market's mid path (via _mid_at), then pooled across markets — the
    honest way to grow the sample: markout is queue- and market-independent per fill. Horizons with
    no marked fills are omitted. Pure (no I/O) so the net-question arithmetic is unit-testable."""
    rows: list[tuple[int, int, float, float]] = []
    for h in MARKOUT_HORIZONS:
        marks, nets = [], []
        for p in pilots:
            for f in p.fills:
                m_h = _mid_at(p, f.ts + h)
                if m_h is None:
                    continue
                marks.append(f.side * (m_h - f.mid_at_fill) * 100.0)  # markout (cents)
                nets.append(f.side * (m_h - f.price) * 100.0)  # edge + markout
        if marks:
            rows.append((h, len(marks), sum(marks) / len(marks), sum(nets) / len(nets)))
    return rows


def report(pilots: list[Pilot]) -> None:
    """Pooled report over one-or-many quoted markets. Each fill's markout is computed against
    ITS OWN market's mid path, then the markout/net numbers are pooled across markets — pooling is
    the whole point of the multi-market mode: it grows the fill sample (tighter markout estimate)
    without mixing mid trajectories. Single-market output is unchanged."""
    multi = len(pilots) > 1
    ident = pilots[0].ticker if not multi else f"{len(pilots)} markets"
    starts = [p.mids[0][0] for p in pilots if p.mids]
    ends = [p.mids[-1][0] for p in pilots if len(p.mids) > 1]
    dur = (max(ends) - min(starts)) / 60.0 if ends and starts else 0.0
    n_polls = sum(p.n_polls for p in pilots)
    spread_sum = sum(p.spread_sum for p in pilots)
    all_fills = [f for p in pilots for f in p.fills]
    n = len(all_fills)
    buys = sum(f.side > 0 for f in all_fills)
    sells = sum(f.side < 0 for f in all_fills)
    net_inv = sum(f.side for f in all_fills)
    print("\n" + "=" * 70)
    print(f"PAPER LP PILOT — {ident}")
    print("=" * 70)
    print(
        f"ran {dur:.1f} min, {n_polls} polls, avg spread "
        f"{100 * spread_sum / max(n_polls, 1):.1f}c"
    )
    print(f"(upper-bound) fills: {n}   buys: {buys}   sells: {sells}   net inventory: {net_inv}")
    if n == 0:
        print("No fills — market(s) too quiet this session; try busier markets/a longer run.")
        return

    edge = sum(f.side * (f.mid_at_fill - f.price) for f in all_fills) * 100.0  # cents
    print(
        f"\ngross edge captured (Σ side·(mid−fill)) : {edge:+.1f}c over {n} fills "
        f"= {edge / n:+.2f}c/fill"
    )
    print(f"{'horizon':>8}{'fills w/ mark':>15}{'mean markout':>15}{'mean net pnl':>15}")
    print("-" * 53)
    for h, n_marks, mean_mark, mean_net in markout_rows(pilots):
        print(f"{h:>6}s{n_marks:>15}{mean_mark:>+14.2f}c{mean_net:>+14.2f}c")

    if multi:
        print(f"\n{'per-market':>28}{'fills':>8}{'net inv':>9}{'gross/fill':>12}")
        for p in sorted(pilots, key=lambda q: -len(q.fills)):
            if not p.fills:
                continue
            g = sum(f.side * (f.mid_at_fill - f.price) for f in p.fills) * 100.0 / len(p.fills)
            inv = sum(f.side for f in p.fills)
            print(f"{p.ticker[:28]:>28}{len(p.fills):>8}{inv:>+9}{g:>+11.2f}c")

    print("\nRead: markout<0 = adverse selection (toxic); net pnl = edge + markout per")
    print("fill. POSITIVE net across horizons => a maker plausibly profits here -> a real")
    print("Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only")
    print("live resting orders (Phase B) give the true rate. Markout is queue-independent.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--prefix", default=None,
                    help="restrict the auto-pick to these comma-separated prefixes (e.g. soccer)")
    ap.add_argument("--category", default=None,
                    help="pick the most-active market in these Kalshi categories (e.g. Politics)")
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--poll", type=float, default=POLL_SECONDS)
    ap.add_argument("--markets", type=int, default=1,
                    help="quote this many of the most-active makeable favorites at once "
                         "(pools fills → the slow politics markout accumulates far faster)")
    args = ap.parse_args()

    client = KalshiClient(pace_seconds=0.1)
    prefixes = tuple(p.strip() for p in args.prefix.split(",")) if args.prefix else None
    series_set: set[str] | None = None
    if args.category:
        cats = {c.strip() for c in args.category.split(",")}
        series_set = {s["ticker"] for s in client.list_series() if s.get("category") in cats}
    if args.ticker:
        tickers = [args.ticker]
    else:
        tickers = pick_benign_tickers(client, prefixes, series_set, n=max(1, args.markets))
    if not tickers:
        print("No actively-trading benign market found right now. Pass --ticker.")
        return 1
    label = tickers[0] if len(tickers) == 1 \
        else f"{len(tickers)} markets ({', '.join(tickers[:3])}…)"
    print(f"Paper-quoting {label} for {args.minutes:.0f} min (poll {args.poll:.0f}s) ...")

    pilots = [Pilot(ticker=tk) for tk in tickers]
    end = time.time() + args.minutes * 60
    sweeps = 0
    while time.time() < end:
        t0 = time.time()
        for p in pilots:
            try:
                poll_once(client, p)
            except Exception as exc:  # keep the session alive through transient API hiccups
                print(f"  poll error [{p.ticker}]: {str(exc)[:80]}")
        sweeps += 1
        if sweeps % 15 == 0:
            print(f"  {sweeps} sweeps, {sum(len(p.fills) for p in pilots)} fills so far")
        time.sleep(max(0.0, args.poll - (time.time() - t0)))
    client.close()
    report(pilots)
    return 0


if __name__ == "__main__":
    sys.exit(main())
