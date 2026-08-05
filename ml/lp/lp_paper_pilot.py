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
    uv run python -m ml.lp.lp_paper_pilot --minutes 20
    uv run python -m ml.lp.lp_paper_pilot --ticker KXMLBGAME-26JUN15... --minutes 30
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

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


def pick_benign_ticker(client: KalshiClient, prefixes: tuple[str, ...] | None = None) -> str | None:
    """Most actively-trading benign market right now, from the live trade feed. ``prefixes``
    restricts the universe (e.g. club-soccer prefixes for a targeted soccer test); default =
    the broad benign board."""
    pfx = prefixes or ELIGIBLE_PREFIXES
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    counts: Counter[str] = Counter()
    for t in trades:
        tk = t.get("ticker", "")
        if any(x in tk for x in EXCLUDE):
            continue
        if any(tk.startswith(p) for p in pfx):
            counts[tk] += 1
    for tk, _ in counts.most_common(25):
        book = client.get_market_orderbook(tk)
        ba = best_bid_ask(book)
        if ba and 0.02 <= (ba[1] - ba[0]) <= 0.15:  # 2-15c spread = makeable, not broken
            return tk
    return None


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


def report(p: Pilot) -> None:
    dur = (p.mids[-1][0] - p.mids[0][0]) / 60.0 if len(p.mids) > 1 else 0.0
    n = len(p.fills)
    print("\n" + "=" * 70)
    print(f"PAPER LP PILOT — {p.ticker}")
    print("=" * 70)
    print(
        f"ran {dur:.1f} min, {p.n_polls} polls, avg spread "
        f"{100 * p.spread_sum / max(p.n_polls, 1):.1f}c"
    )
    print(
        f"(upper-bound) fills: {n}   buys: {sum(f.side > 0 for f in p.fills)}   "
        f"sells: {sum(f.side < 0 for f in p.fills)}   net inventory: {sum(f.side for f in p.fills)}"
    )
    if n == 0:
        print("No fills — market too quiet this session; try a busier market/longer run.")
        return

    edge = sum(f.side * (f.mid_at_fill - f.price) for f in p.fills) * 100.0  # cents
    print(
        f"\ngross edge captured (Σ side·(mid−fill)) : {edge:+.1f}c over {n} fills "
        f"= {edge / n:+.2f}c/fill"
    )
    print(f"{'horizon':>8}{'fills w/ mark':>15}{'mean markout':>15}{'mean net pnl':>15}")
    print("-" * 53)
    for h in MARKOUT_HORIZONS:
        marks, nets = [], []
        for f in p.fills:
            m_h = _mid_at(p, f.ts + h)
            if m_h is None:
                continue
            marks.append(f.side * (m_h - f.mid_at_fill) * 100.0)  # markout (cents)
            nets.append(f.side * (m_h - f.price) * 100.0)  # edge + markout
        if marks:
            print(
                f"{h:>6}s{len(marks):>15}{sum(marks) / len(marks):>+14.2f}c"
                f"{sum(nets) / len(nets):>+14.2f}c"
            )

    print("\nRead: markout<0 = adverse selection (toxic); net pnl = edge + markout per")
    print("fill. POSITIVE net across horizons => a maker plausibly profits here -> a real")
    print("Phase-B candidate. Reminder: fill rate is an UPPER BOUND (queue ignored); only")
    print("live resting orders (Phase B) give the true rate. Markout is queue-independent.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--prefix", default=None,
                    help="restrict the auto-pick to these comma-separated prefixes (e.g. soccer)")
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--poll", type=float, default=POLL_SECONDS)
    args = ap.parse_args()

    client = KalshiClient(pace_seconds=0.1)
    prefixes = tuple(p.strip() for p in args.prefix.split(",")) if args.prefix else None
    ticker = args.ticker or pick_benign_ticker(client, prefixes)
    if not ticker:
        print("No actively-trading benign market found right now. Pass --ticker.")
        return 1
    print(f"Paper-quoting {ticker} for {args.minutes:.0f} min (poll {args.poll:.0f}s) ...")

    p = Pilot(ticker=ticker)
    end = time.time() + args.minutes * 60
    while time.time() < end:
        t0 = time.time()
        try:
            poll_once(client, p)
        except Exception as exc:  # keep the session alive through transient API hiccups
            print(f"  poll error: {str(exc)[:80]}")
        if p.n_polls % 15 == 0 and p.n_polls:
            print(f"  {p.n_polls} polls, {len(p.fills)} fills so far")
        time.sleep(max(0.0, args.poll - (time.time() - t0)))
    client.close()
    report(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
