"""Controlled A/B: inventory SKEW on vs off, on the live club-soccer board (READ-ONLY, $0).

THE question this settles: is the maker doing genuine TWO-SIDED spread capture, or just
accumulating a directional bet that the inventory cap happens to bound?

Why an A/B and not two separate runs: markets, scorelines and volatility differ minute to
minute, so comparing across sessions confounds skew with conditions. Here ONE book+trades
fetch per ticker per sweep is handed to BOTH arms, so they see identical markets, identical
prints, identical timestamps — the only difference is `skew_per_contract`. Any divergence is
attributable to skew alone.

Background (2026-09-05/06): every paper run pegged inventory at the cap, which made every
paper P&L an inventory mark rather than capture. Root cause was that `lp_live` skews quotes
to mean-revert inventory to flat while `lp_pilot` did not — the sim was STRICTLY more
aggressive than the live bot. First A/B (3 markets, 9 min): SKEW ON pegged 0/3 with mean
|inv| 1.3 and net +1.95c/fill at 60s; SKEW OFF pegged 3/3 with mean |inv| 10.0 and net
-0.01c at 60s. This module exists to see whether that reproduces across sessions and
scorelines — one session is a promising read, not a fact.

Read the output in this order:
  1. PEGGED / mean |inv|  — is inventory actually two-sided, or pinned?
  2. net/fill by horizon  — does capture survive after adverse selection?
  3. markout              — the queue-INDEPENDENT toxicity signal (fills are upper-bounded).

Usage::

    uv run python -m strategies.soccer_mm.skew_ab --minutes 20 --markets 6
    uv run python -m strategies.soccer_mm.skew_ab --prefix KXEPL,KXLALIGA --minutes 30
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from core.maker.lp_pilot import (
    DAILY_LOSS_LIMIT,
    MARKOUT_HORIZONS,
    MAX_POSITION,
    SKEW_PER_CONTRACT,
    Fill,
    Pilot,
    _f,
    _mid_at,
    best_bid_ask,
    pick_smooth_tickers,
    pnl,
)
from ingestion.kalshi import KalshiClient

SOCCER_PREFIXES = ("KXEPL", "KXLALIGA", "KXSERIEA", "KXBUNDESLIGA", "KXLIGUE1",
                   "KXUCL", "KXUEL", "KXMLS", "KXLIGAMX", "KXBRASILEIRO",
                   "KXEREDIVISIE", "KXEFLCHAMPIONSHIP")


def apply_poll(p: Pilot, now: float, bid: float, ask: float, mid: float,
               trades: list[dict[str, Any]], skew_pc: float) -> None:
    """`lp_pilot.poll_once` logic on PRE-FETCHED data, so both arms see identical inputs.
    Mirrors lp_live's skew: push the ACCUMULATING side off the touch by skew_pc*|inv|, leave
    the REDUCING side at the touch. Recomputed per trade — inventory moves within a poll."""
    p.mids.append((now, mid))
    p.n_polls += 1
    p.spread_sum += ask - bid
    for t in trades:
        tid = t.get("trade_id")
        if not tid or tid in p.seen:
            continue
        p.seen.add(tid)
        px = _f(t.get("yes_price_dollars"))
        if px is None:
            continue
        skew = skew_pc * p.inv
        bid_px = round(max(0.01, min(0.99, bid - max(0.0, skew))), 2)
        ask_px = round(max(0.01, min(0.99, ask - min(0.0, skew))), 2)
        if px <= bid_px and p.inv < MAX_POSITION:
            p.fills.append(Fill(now, +1, bid_px, mid))
            p.inv += 1
            p.cash -= bid_px
        elif px >= ask_px and p.inv > -MAX_POSITION:
            p.fills.append(Fill(now, -1, ask_px, mid))
            p.inv -= 1
            p.cash += ask_px
    p.max_abs_inv = max(p.max_abs_inv, abs(p.inv))
    p.min_pnl = min(p.min_pnl, pnl(p, mid))
    if pnl(p, mid) <= -DAILY_LOSS_LIMIT:
        p.halted = True


def summarize(name: str, pilots: list[Pilot]) -> None:
    live = [p for p in pilots if p.mids]
    if not live:
        print(f"\n--- {name} ---\n  (no book data)")
        return
    fills = sum(len(p.fills) for p in live)
    pegged = sum(1 for p in live if p.max_abs_inv >= MAX_POSITION)
    pooled = sum(pnl(p, p.mids[-1][1]) for p in live)
    mean_abs = sum(abs(p.inv) for p in live) / len(live)
    mean_max = sum(p.max_abs_inv for p in live) / len(live)
    print(f"\n--- {name} ---")
    print(f"  fills {fills}   pooled P&L ${pooled:+.2f}   PEGGED {pegged}/{len(live)}"
          f"   mean|inv| {mean_abs:.1f}   mean max|inv| {mean_max:.1f} (cap {MAX_POSITION})")
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
            print(f"    {h:>3}s  n={len(marks):>5}  markout {sum(marks) / len(marks):+.2f}c"
                  f"   net {sum(nets) / len(nets):+.2f}c")
    print("    per-market max|inv|: " + ", ".join(
        f"{p.ticker[-14:]}={p.max_abs_inv}{'*' if p.max_abs_inv >= MAX_POSITION else ''}"
        for p in sorted(live, key=lambda x: -x.max_abs_inv)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--markets", type=int, default=6)
    ap.add_argument("--poll", type=float, default=6.0)
    ap.add_argument("--prefix", default=None, help="comma-separated series prefixes")
    args = ap.parse_args()

    prefixes = (tuple(p.strip() for p in args.prefix.split(","))
                if args.prefix else SOCCER_PREFIXES)
    client = KalshiClient(pace_seconds=0.05)
    tickers = pick_smooth_tickers(client, args.markets, prefixes)
    if not tickers:
        print("No makeable club-soccer markets right now (no game in play, or all books "
              "competed to 1c / below the recent-trade floor).")
        return 1
    print(f"SKEW A/B on {len(tickers)} markets for {args.minutes:.0f} min "
          f"(skew {SKEW_PER_CONTRACT} vs 0.0), identical inputs to both arms:")
    for t in tickers:
        print(f"   {t}")

    skew_arm = {t: Pilot(ticker=t) for t in tickers}
    flat_arm = {t: Pilot(ticker=t) for t in tickers}
    end = time.time() + args.minutes * 60
    sweeps = 0
    while time.time() < end:
        t0 = time.time()
        for tk in tickers:
            try:
                ba = best_bid_ask(client.get_market_orderbook(tk))
                if ba is None:
                    continue
                bid, ask = ba
                trades = client.get("/markets/trades",
                                    params={"ticker": tk, "limit": 100}).get("trades", [])
            except Exception:
                continue  # a transient failure must not kill the session
            now, mid = time.time(), (bid + ask) / 2.0
            apply_poll(skew_arm[tk], now, bid, ask, mid, trades, SKEW_PER_CONTRACT)
            apply_poll(flat_arm[tk], now, bid, ask, mid, trades, 0.0)
        sweeps += 1
        if sweeps % 20 == 0:
            ps = sum(1 for p in skew_arm.values() if p.max_abs_inv >= MAX_POSITION)
            pf = sum(1 for p in flat_arm.values() if p.max_abs_inv >= MAX_POSITION)
            print(f"  {sweeps} sweeps | pegged: skew {ps}/{len(tickers)}, flat {pf}/{len(tickers)}")
        time.sleep(max(0.0, args.poll - (time.time() - t0)))
    client.close()

    print("\n" + "=" * 78)
    print(f"SKEW A/B — {len(tickers)} markets, {sweeps} sweeps, identical inputs")
    print("=" * 78)
    summarize(f"SKEW ON ({SKEW_PER_CONTRACT}/contract) = what lp_live does",
              list(skew_arm.values()))
    summarize("SKEW OFF (0.0) = no inventory lean", list(flat_arm.values()))
    print("\nRead: two-sided capture iff SKEW ON keeps PEGGED at 0 and mean|inv| near flat "
          "\nWHILE net/fill stays positive at 60s. Fill counts are an UPPER bound (queue "
          "\nignored); markout is queue-independent and is the trustworthy toxicity signal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
