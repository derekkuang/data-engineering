"""LP pilot v2 — inventory-managed paper market-maker (the pre-live safety gate).

v1 (core/maker/lp_paper_pilot.py) showed a benign retail market (markout ~0, ~1c/fill) but
drifted to -137 one-sided inventory because it quoted both sides blindly — i.e. it
became a directional bet, not spread capture. v2 fixes that and bakes in the LIVE
risk caps so the same code is what we'd flip to real orders:

  * INVENTORY CAP (+/-MAX_POSITION): if we're at the long cap we stop quoting the
    bid (only offer to sell down); at the short cap we stop quoting the ask. So
    inventory is forced back toward flat -> real two-sided spread capture.
  * KILL SWITCH: mark-to-mid P&L = cash + inventory*mid; if it drops below
    -DAILY_LOSS_LIMIT the session halts (this is the live safety net, exercised on
    paper here).
  * SMOOTH-MARKET SELECTION: skip discrete-jump props (BTTS/goals/corners/cards)
    so the fill/markout read isn't contaminated by event jumps.

Paper mode only (fills simulated; no auth, no money). It is the exact skeleton of
the live bot — Phase B swaps the simulated fill for a real resting order, and the
caps here ARE the live caps. Fill rate remains an UPPER BOUND (queue ignored);
markout and the inventory/PnL behaviour are the trustworthy outputs.

Usage:
    uv run python -m core.maker.lp_pilot --minutes 20
    uv run python -m core.maker.lp_pilot --ticker KX... --minutes 30
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.maker.lp_gate import passes_gate
from ingestion.kalshi import KalshiClient

POLL_SECONDS = 4.0
MARKOUT_HORIZONS = (15, 30, 60)  # seconds
MAX_POSITION = 20  # contracts, +/- (was 10; doubled with QUOTE_SIZE=2 for the 2x scale test)
DAILY_LOSS_LIMIT = 10.0  # dollars; kill switch per-market AND session (was 5; 2x with size)
# Quote/hold a market only while its mid is in [MIN_MID, MAX_MID]. Used by BOTH
# pick_smooth_ticker (entry) and lp_live._run_market (the "extreme" exit).
# UPPER CAP REINSTATED 0.92 (2026-06-18): the no-cap experiment showed its cost — gated
# (no-ITF) markets that rode to 0.91-0.98 got picked off hard (markout -6 to -8.6c on
# WNBA 1H-winners / MLB games marching to a favorite). Capping at 0.92 makes us exit +
# flatten before the most one-sided last few cents. Lower stays 0.05 (longshots are cheap).
MIN_MID, MAX_MID = 0.05, 0.92
# COARSE pre-filter only — the universe of sports whose books we might quote. This is NOT the
# authority on WHAT is safe to make: several prefixes here (KXMLB/KXWNBA/KXITF...) were MEASURED
# toxic. The authoritative, fail-CLOSED gate is the per-family verdict in quotable_families.json,
# enforced in lp_gate.passes_gate; this list just keeps the trades-feed scan cheap. (Was
# BENIGN_PREFIXES — renamed 2026-07-25 when the verdict loop was closed, since it is not benign.)
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
    "KXTENNIS",
    "KXGOLF",
    "KXRT",
    # Club soccer — the post-WC primary hypothesis (probed 2026-07-05: SPREAD/TOTAL series
    # exist for all of these). Summer leagues are live now; European majors from ~mid-August.
    "KXMLS",
    "KXLIGAMX",
    "KXBRASILEIRO",
    "KXALLSVENSKAN",
    "KXELITESERIEN",
    "KXDENSUPERLIGA",
    "KXCOPADOBRASIL",
    "KXEPL",
    "KXLALIGA",
    "KXSERIEA",
    "KXBUNDESLIGA",
    "KXLIGUE1",
    "KXUCL",
    "KXUEL",
    "KXEREDIVISIE",
    "KXEFLCHAMPIONSHIP",
    "KXSAUDIPL",
)
EXCLUDE = ("15M", "PERP", "KXBTC", "KXETH", "KXSOL", "KXHYPE", "KXXRP", "KXDOGE")
# Discrete-jump prop types to skip for a "smooth" market (price lurches on an event).
JUMPY = (
    "BTTS",
    "GOAL",
    "CORNER",
    "CARD",
    "PEN",
    "SCORE",
    "ASSIST",
    "REDCARD",
    "FIRSTTO",
    "RFI",
    "INNING",
    "FIRSTINNING",
    "MENTION",  # "will X be mentioned" — thin/jumpy prop; bled -16.5c markout 2026-06-17
    "SOA",  # shots-on-target style props — same thin/jumpy class
)  # RFI = run-first-inning etc. resolve too fast
# We make markets ONLY in mean-reverting types: TOTAL (over/under a line) and SPREAD
# (margin vs a line) oscillate around the line, so two-sided quoting captures the spread
# cleanly (active total/spread = +0.76c/fill, ~all the realized edge). Everything else —
# moneyline/winner/match GAMES, half-winners, directional props (home runs, strikeouts,
# golf round-leader) — trends to a near-certain outcome and picks the maker off (the
# 2026-06-18 overnight lost -$5 in trending ATP). So this is an ALLOWLIST: a ticker we
# don't recognize as TOTAL/SPREAD is NOT quoted. pick_smooth_ticker idles if none active.
MEAN_REVERTING_TYPES = ("TOTAL", "SPREAD")


def _f(v: Any) -> float | None:
    return float(v) if v is not None else None


@dataclass
class Fill:
    ts: float
    side: int  # +1 bought (long), -1 sold (short)
    price: float
    mid_at_fill: float


@dataclass
class Pilot:
    ticker: str
    mids: list[tuple[float, float]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    inv: int = 0
    cash: float = 0.0  # signed cash flow: -price on a buy, +price on a sell
    max_abs_inv: int = 0
    min_pnl: float = 0.0
    n_polls: int = 0
    spread_sum: float = 0.0
    halted: bool = False


def best_bid_ask(book: dict[str, Any]) -> tuple[float, float] | None:
    yes = book.get("yes_dollars") or book.get("yes") or []
    no = book.get("no_dollars") or book.get("no") or []
    yb = [float(p) for p, _ in yes] if yes else []
    nb = [float(p) for p, _ in no] if no else []
    if not yb or not nb:
        return None
    return max(yb), round(1.0 - max(nb), 4)


def is_mean_reverting(ticker: str) -> bool:
    """True only for TOTAL/SPREAD markets — the ones that oscillate around a line and are
    safe to two-sided quote. Everything else trends to a winner and picks the maker off."""
    return any(t in ticker for t in MEAN_REVERTING_TYPES)


TRADE_WINDOW_MIN = 5.0  # minutes; the window the per-market activity rate is measured over


def recent_trade_rate(
    client: KalshiClient, ticker: str, window_min: float = TRADE_WINDOW_MIN
) -> int:
    """Trades on THIS market in the last `window_min` minutes, measured per-ticker.

    WHY (measured 2026-09-05): the global ``/markets/trades?limit=1000`` tape is a FIXED-SIZE
    window shared by the whole exchange, so a busy unrelated series crowds everything else out
    of it — KXBTC15M alone held 322/1000 slots, crypto ~45%. A Bundesliga TOTAL book with 63
    real prints/5min showed as ~5 on the tape and failed the >=15 gate, so the maker idled on a
    genuinely makeable market (and `discover_markets` under-captured it — the likely mechanism
    behind soccer sitting at 5-7 capture days). ``/markets/trades`` accepts a ``ticker`` filter,
    which gives the market's OWN prints and is immune to what else is trading. Note the
    per-ticker rows carry ``created_time`` (ISO) and a null ``ts`` — read created_time.
    """
    try:
        d = client.get("/markets/trades", params={"ticker": ticker, "limit": 200})
    except Exception:
        return 0
    cutoff = datetime.now(UTC) - timedelta(minutes=window_min)
    n = 0
    for t in d.get("trades", []):
        raw = str(t.get("created_time") or "")
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when >= cutoff:
            n += 1
    return n


def enumerate_candidates(
    client: KalshiClient, prefixes: tuple[str, ...], *, min_volume: float = 500.0,
    cap: int = 40,
) -> list[str]:
    """Mean-reverting, two-sided, real-volume markets for `prefixes` — a tape-INDEPENDENT
    candidate source used when the shared trades tape is crowded out (see `recent_trade_rate`).

    Enumerates per SERIES, because the makeable markets live on their own series tickers:
    ``KX<LEAGUE>GAME`` carries only the 3-way match result (directional, never quoted), while
    the TOTAL/SPREAD books we make are ``KX<LEAGUE>TOTAL`` / ``KX<LEAGUE>SPREAD`` (+1H
    variants). Paging /markets does NOT work here — it is not volume-ordered and soccer never
    surfaces. Volume-ranked; prices are dollar STRINGS (yes_bid_dollars)."""
    suffixes = ("TOTAL", "SPREAD", "1HTOTAL", "1HSPREAD")
    rows: list[tuple[float, str]] = []
    for pfx in prefixes:
        for suf in suffixes:
            try:
                d = client.get("/events", params={
                    "series_ticker": f"{pfx}{suf}", "status": "open",
                    "with_nested_markets": "true", "limit": 40})
            except Exception:
                continue
            for e in d.get("events", []):
                for m in e.get("markets", []) or []:
                    tk = str(m.get("ticker", ""))
                    if not tk or any(x in tk for x in EXCLUDE) or any(j in tk for j in JUMPY):
                        continue
                    if not is_mean_reverting(tk):
                        continue
                    if m.get("yes_bid_dollars") is None or m.get("yes_ask_dollars") is None:
                        continue
                    vol = float(m.get("volume_24h_fp") or 0.0)
                    if vol < min_volume:
                        continue
                    rows.append((vol, tk))
    rows.sort(reverse=True)
    return [tk for _, tk in rows[:cap]]


def pick_smooth_ticker(
    client: KalshiClient,
    exclude: set[str] | None = None,
    prefixes: tuple[str, ...] | None = None,
) -> str | None:
    """Most active benign, NON-jumpy, gate-eligible, MEAN-REVERTING market with a makeable
    2-15c spread. ``exclude`` skips already-used/retired tickers (for rolling). ``prefixes``
    restricts the eligible ticker prefixes (default ELIGIBLE_PREFIXES); pass e.g. ("KXWC",) to
    quote World-Cup-only — the structural finding that soccer's rare-discrete scoring is the
    only consistently benign cell (basketball/baseball continuous scoring -> toxic).

    The SELECTION GATE (``core.maker.lp_gate.passes_gate``) drops structurally-toxic types (ITF)
    and books below the recent-trade floor. On top of that we quote ONLY mean-reverting
    types (totals/spreads) and NEVER trending winner/moneyline/match markets — if none are
    active (e.g. overnight US-time, when only low-tier trending tennis trades) this returns
    None and the caller IDLES rather than bleeding into pick-off books. The 2026-06-18
    overnight lost -$5 doing exactly that (68/84 markets were trending ATP)."""
    skip = exclude or set()
    pfx = prefixes or ELIGIBLE_PREFIXES
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    counts: Counter[str] = Counter()
    for t in trades:
        tk = t.get("ticker", "")
        if tk in skip or any(x in tk for x in EXCLUDE) or any(j in tk for j in JUMPY):
            continue
        if any(tk.startswith(p) for p in pfx):
            counts[tk] += 1
    # Candidates: the shared tape first (cheap, volume-ordered), then a tape-INDEPENDENT
    # enumeration so a crowded tape can't hide makeable books (see `recent_trade_rate`).
    ordered = [tk for tk, _ in counts.most_common(25)]
    seen = set(ordered)
    ordered += [tk for tk in enumerate_candidates(client, pfx) if tk not in seen and tk not in skip]
    for tk in ordered:
        if not is_mean_reverting(tk):  # quote ONLY totals/spreads — everything else trends
            continue                   # and picks us off (overnight that's all that's active)
        # Gate on the market's OWN recent prints, not its share of the shared tape: the tape
        # undercounts anything competing with a high-frequency series and would idle us on a
        # genuinely active book (measured: 63 prints/5min reading as ~5 on the tape).
        if not passes_gate(tk, recent_trade_rate(client, tk)):
            continue
        ba = best_bid_ask(client.get_market_orderbook(tk))
        if ba is None:
            continue
        spread, mid = ba[1] - ba[0], (ba[0] + ba[1]) / 2.0
        # makeable spread AND inside the MIN_MID/MAX_MID band (away from one-sided/resolved)
        if 0.02 <= spread <= 0.15 and MIN_MID <= mid <= MAX_MID:
            return tk
    return None  # nothing mean-reverting active -> idle, don't fall into trending books


def pick_smooth_tickers(
    client: KalshiClient, n: int, prefixes: tuple[str, ...] | None = None,
    *, max_per_event: int = 2,
) -> list[str]:
    """Up to `n` makeable, gate-passing, mean-reverting markets — the multi-market analogue of
    `pick_smooth_ticker`, with the SAME gates (per-market activity, TOTAL/SPREAD only, spread
    and mid bands).

    DIVERSIFY ACROSS GAMES (`max_per_event`): the buckets of one match (Over 1.5/2.5/3.5) are
    the SAME bet — one goal moves them together — so N slots from one game give N x the
    exposure, not N independent samples. Measured 2026-09-05: a single-market paper run pinned
    inventory at the cap for its whole session because every fill was one directional episode.
    Keying on the match segment (KXEPLTOTAL-26SEP05MCICOV-4 -> 26SEP05MCICOV) makes
    `n` markets mean ~n/max_per_event distinct games."""
    pfx = prefixes or ELIGIBLE_PREFIXES
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    counts: Counter[str] = Counter()
    for t in trades:
        tk = t.get("ticker", "")
        if tk and not any(x in tk for x in EXCLUDE) and not any(j in tk for j in JUMPY):
            if any(tk.startswith(p) for p in pfx):
                counts[tk] += 1
    ordered = [tk for tk, _ in counts.most_common(50)]
    seen = set(ordered)
    ordered += [tk for tk in enumerate_candidates(client, pfx) if tk not in seen]

    picked: list[str] = []
    per_event: Counter[str] = Counter()
    for tk in ordered:
        if len(picked) >= n:
            break
        if not is_mean_reverting(tk):
            continue
        parts = tk.split("-")
        event = parts[1] if len(parts) > 1 else tk
        if per_event[event] >= max_per_event:
            continue
        if not passes_gate(tk, recent_trade_rate(client, tk)):
            continue
        ba = best_bid_ask(client.get_market_orderbook(tk))
        if ba is None:
            continue
        spread, mid = ba[1] - ba[0], (ba[0] + ba[1]) / 2.0
        if 0.02 <= spread <= 0.15 and MIN_MID <= mid <= MAX_MID:
            picked.append(tk)
            per_event[event] += 1
    return picked


def report_multi(pilots: list[Pilot]) -> None:
    """Pooled report over many inventory-CAPPED markets. Each fill is marked against ITS OWN
    market's mid path, then pooled — so the markout is a cross-game average rather than one
    game's directional episode. The per-market table is the honest check: with caps working,
    max|inv| should sit BELOW the cap and inventory should not sit pegged."""
    live = [p for p in pilots if p.mids]
    if not live:
        print("No market produced a book this session.")
        return
    dur = max((p.mids[-1][0] - p.mids[0][0]) / 60.0 for p in live)
    polls = sum(p.n_polls for p in live)
    spread_c = 100.0 * sum(p.spread_sum for p in live) / max(polls, 1)
    n_fills = sum(len(p.fills) for p in live)
    pooled_pnl = sum(pnl(p, p.mids[-1][1]) for p in live)
    n_halt = sum(1 for p in live if p.halted)
    print("\n" + "=" * 78)
    print(f"LP PILOT v2 — INVENTORY-CAPPED, {len(live)} markets")
    print("=" * 78)
    print(f"ran {dur:.1f} min, {polls} polls, avg spread {spread_c:.1f}c"
          f"{f'   [{n_halt} market(s) HALTED]' if n_halt else ''}")
    print(f"(upper-bound) fills: {n_fills}   pooled mark-to-mid P&L: ${pooled_pnl:+.2f}   "
          f"cap +/-{MAX_POSITION}")
    if not n_fills:
        print("No fills — markets too quiet this session.")
        return
    print(f"\n{'horizon':>8}{'fills w/ mark':>15}{'mean markout':>15}{'mean net pnl':>15}")
    print("-" * 53)
    for h in MARKOUT_HORIZONS:
        marks, nets = [], []
        for p in live:
            for f in p.fills:
                m_h = _mid_at(p, f.ts + h)
                if m_h is None:
                    continue
                marks.append(f.side * (m_h - f.mid_at_fill) * 100.0)
                nets.append(f.side * (m_h - f.price) * 100.0)
        if marks:
            print(f"{h:>6}s{len(marks):>15}{sum(marks) / len(marks):>+14.2f}c"
                  f"{sum(nets) / len(nets):>+14.2f}c")
    print(f"\n{'per-market':>34}{'fills':>7}{'inv':>6}{'max|inv|':>10}{'pegged?':>9}")
    for p in sorted(live, key=lambda x: -len(x.fills)):
        pegged = "PEGGED" if p.max_abs_inv >= MAX_POSITION else ""
        print(f"{p.ticker[-32:]:>34}{len(p.fills):>7}{p.inv:>+6d}{p.max_abs_inv:>10}"
              f"{pegged:>9}")
    print("\nRead: caps are working iff max|inv| stays BELOW the cap. A market showing PEGGED "
          "\nwas inventory-constrained — its fills are one directional episode, not two-sided "
          "\ncapture, and its P&L should be discounted accordingly.")


def better_market(
    client: KalshiClient,
    current: str,
    exclude: set[str],
    factor: float,
    prefixes: tuple[str, ...] | None = None,
) -> str | None:
    """The most-active gate-eligible, MEAN-REVERTING market right now that is at least
    `factor`x more active than `current` (by recent-trade count), else None. Pure activity
    scan — NO orderbook calls — so it's cheap to run inside the quoting loop to chase flow.
    `exclude` skips retired/forbidden tickers (pass `current` too). `prefixes` restricts the
    eligible prefixes (default ELIGIBLE_PREFIXES; pass ("KXWC",) for World-Cup-only) so the
    switch stays inside the same universe as selection. Trending winner/moneyline markets are
    skipped so the switch can't yank us INTO the pick-off books selection avoids. Returns None
    if nothing qualifies, so the caller stays put (protects queue)."""
    pfx = prefixes or ELIGIBLE_PREFIXES
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    counts: Counter[str] = Counter()
    for t in trades:
        tk = t.get("ticker", "")
        if any(x in tk for x in EXCLUDE) or any(j in tk for j in JUMPY):
            continue
        if any(tk.startswith(p) for p in pfx):
            counts[tk] += 1
    cur = counts.get(current, 0)
    for tk, c in counts.most_common():  # descending — first eligible = most active alt
        if tk in exclude or not passes_gate(tk, c) or not is_mean_reverting(tk):
            continue
        return tk if c >= factor * max(cur, 1) else None
    return None


def pnl(p: Pilot, mid: float) -> float:
    """Mark-to-mid dollar P&L = realized cash + inventory marked at the current mid."""
    return p.cash + p.inv * mid


def poll_once(client: KalshiClient, p: Pilot) -> None:
    now = time.time()
    ba = best_bid_ask(client.get_market_orderbook(p.ticker))
    if ba is None:
        return
    bid, ask = ba
    mid = (bid + ask) / 2.0
    p.mids.append((now, mid))
    p.n_polls += 1
    p.spread_sum += ask - bid

    # Inventory-capped quoting: quote a side only if it won't breach the cap.
    quote_bid = p.inv < MAX_POSITION  # room to buy
    quote_ask = p.inv > -MAX_POSITION  # room to sell

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
        if quote_bid and px <= bid and p.inv < MAX_POSITION:
            p.fills.append(Fill(now, +1, bid, mid))
            p.inv += 1
            p.cash -= bid
        elif quote_ask and px >= ask and p.inv > -MAX_POSITION:
            p.fills.append(Fill(now, -1, ask, mid))
            p.inv -= 1
            p.cash += ask

    p.max_abs_inv = max(p.max_abs_inv, abs(p.inv))
    p.min_pnl = min(p.min_pnl, pnl(p, mid))
    if pnl(p, mid) <= -DAILY_LOSS_LIMIT:  # kill switch
        p.halted = True


def _mid_at(p: Pilot, ts: float) -> float | None:
    for t, m in p.mids:
        if t >= ts:
            return m
    return None


def report(p: Pilot) -> None:
    dur = (p.mids[-1][0] - p.mids[0][0]) / 60.0 if len(p.mids) > 1 else 0.0
    n = len(p.fills)
    final_mid = p.mids[-1][1] if p.mids else 0.0
    print("\n" + "=" * 70)
    print(f"LP PILOT v2 (inventory-managed paper) — {p.ticker}")
    print("=" * 70)
    halt = "   [HALTED by kill switch]" if p.halted else ""
    print(
        f"ran {dur:.1f} min, {p.n_polls} polls, avg spread "
        f"{100 * p.spread_sum / max(p.n_polls, 1):.1f}c{halt}"
    )
    print(
        f"(upper-bound) fills: {n}   final inventory: {p.inv:+d}   "
        f"max |inventory|: {p.max_abs_inv} (cap {MAX_POSITION})"
    )
    print(
        f"mark-to-mid P&L: ${pnl(p, final_mid):+.2f}   worst drawdown: ${p.min_pnl:+.2f} "
        f"(kill at -${DAILY_LOSS_LIMIT:.0f})"
    )
    if n == 0:
        print("No fills this session — try a busier market / longer run.")
        return

    print(f"\n{'horizon':>8}{'fills w/ mark':>15}{'mean markout':>15}{'mean net pnl':>15}")
    print("-" * 53)
    for h in MARKOUT_HORIZONS:
        marks, nets = [], []
        for f in p.fills:
            m_h = _mid_at(p, f.ts + h)
            if m_h is None:
                continue
            marks.append(f.side * (m_h - f.mid_at_fill) * 100.0)
            nets.append(f.side * (m_h - f.price) * 100.0)
        if marks:
            print(
                f"{h:>6}s{len(marks):>15}{sum(marks) / len(marks):>+14.2f}c"
                f"{sum(nets) / len(nets):>+14.2f}c"
            )

    print("\nRead: the win vs v1 is max |inventory| staying <= the cap (no directional")
    print("drift) while net pnl/fill stays positive => spread capture survives inventory")
    print("control. If so, this is a ready Phase-B (live) candidate — the live flip uses")
    print("THESE caps. Fill rate is still an upper bound; only live resting orders settle it.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--poll", type=float, default=POLL_SECONDS)
    ap.add_argument("--markets", type=int, default=1,
                    help="quote N markets at once, each with its OWN inventory cap + kill "
                         "switch; diversified across games (max 2 per match)")
    ap.add_argument("--prefix", default=None,
                    help="comma-separated series prefixes, e.g. KXEPL,KXLALIGA")
    args = ap.parse_args()

    client = KalshiClient(pace_seconds=0.1)
    prefixes = tuple(p.strip() for p in args.prefix.split(",")) if args.prefix else None

    if args.markets > 1:
        tickers = pick_smooth_tickers(client, args.markets, prefixes)
        if not tickers:
            print("No active makeable markets right now.")
            return 1
        print(f"Paper-quoting {len(tickers)} markets for {args.minutes:.0f} min "
              f"(cap +/-{MAX_POSITION} EACH, kill -${DAILY_LOSS_LIMIT:.0f} each): "
              f"{', '.join(t[-26:] for t in tickers[:3])}...")
        pilots = [Pilot(ticker=t) for t in tickers]
        end = time.time() + args.minutes * 60
        sweeps = 0
        while time.time() < end and any(not p.halted for p in pilots):
            t0 = time.time()
            for p in pilots:
                if p.halted:
                    continue
                try:
                    poll_once(client, p)
                except Exception as exc:
                    print(f"  {p.ticker[-20:]} poll error: {str(exc)[:60]}")
            sweeps += 1
            if sweeps % 10 == 0:
                pegged = sum(1 for p in pilots if p.max_abs_inv >= MAX_POSITION)
                print(f"  {sweeps} sweeps, {sum(len(p.fills) for p in pilots)} fills, "
                      f"{pegged}/{len(pilots)} pegged")
            time.sleep(max(0.0, args.poll - (time.time() - t0)))
        client.close()
        report_multi(pilots)
        return 0

    ticker = args.ticker or pick_smooth_ticker(client, prefixes=prefixes)
    if not ticker:
        print("No active smooth benign market found. Pass --ticker.")
        return 1
    print(
        f"Paper-quoting {ticker} for {args.minutes:.0f} min "
        f"(cap +/-{MAX_POSITION}, kill -${DAILY_LOSS_LIMIT:.0f}) ..."
    )

    p = Pilot(ticker=ticker)
    end = time.time() + args.minutes * 60
    while time.time() < end and not p.halted:
        t0 = time.time()
        try:
            poll_once(client, p)
        except Exception as exc:
            print(f"  poll error: {str(exc)[:80]}")
        if p.n_polls % 15 == 0 and p.n_polls:
            print(f"  {p.n_polls} polls, {len(p.fills)} fills, inv {p.inv:+d}")
        time.sleep(max(0.0, args.poll - (time.time() - t0)))
    client.close()
    report(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
