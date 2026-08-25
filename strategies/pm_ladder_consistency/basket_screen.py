"""P1 — NegRisk MECE basket-consistency screen (READ-ONLY, $0).

THE question: does fee-free Polymarket leave executable complement arbs on its
mutually-exclusive-and-exhaustive fields (Fed-decision ladders, "largest company",
election-winner fields, tweet-count buckets)? On a MECE field the Yes legs must sum to
$1; if you can BUY every leg for ``sum_ask < 1`` you bank ``1 - sum_ask`` risk-free
(fees are zero here — the one structural advantage over Kalshi, whose fee traps exactly
this kind of ladder mispricing; see ``strategies/btc_direction/threshold_arb.py``).

This is the fee-free twin of our Kalshi threshold-arb null. First live read (2026-08-24,
snapshot) said the seam is bot-sealed: median ``sum_ask`` ~1.014, ``sum_bid`` ~0.993
across the liquid fields — buying a basket costs ~1.3c over par, selling pays ~0.7c
under. This module makes that measurement repeatable and honest (liveness-gated, so dead
day-of ladders don't masquerade as arbs), with the depth of any surviving edge.

Usage::

    uv run python -m strategies.pm_ladder_consistency.basket_screen
    uv run python -m strategies.pm_ladder_consistency.basket_screen --limit 250 --max-outcomes 40
    uv run python -m strategies.pm_ladder_consistency.basket_screen --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from typing import Any

from ingestion import polymarket as pm

# An executable buy-basket arb must clear typical taker slippage; below this the "edge"
# is inside the touch and vanishes on the second leg. Zero fees, so no fee term.
ARB_THRESHOLD = 0.005  # 0.5c per $1 set


def scan(
    *, limit: int, max_outcomes: int, min_volume: float
) -> list[pm.BasketQuote]:
    """Score every liquid NegRisk MECE field. Skips >max_outcomes fields (the 128-leg
    nomination markets are one huge POST but rarely the arb; cap keeps the scan quick)."""
    out: list[pm.BasketQuote] = []
    with pm.client() as c:
        events = pm.fetch_events(c, limit=limit)
        fields = []
        for e in events:
            if not e.get("negRisk") or float(e.get("volume24hr") or 0) < min_volume:
                continue
            n_live = len([m for m in (e.get("markets") or []) if not m.get("closed")])
            if 3 <= n_live <= max_outcomes:
                fields.append(e)
        for e in fields:
            try:
                bq = pm.basket_quote(c, e)
            except Exception as exc:  # a single bad field must not kill the sweep
                print(f"  skip {e.get('slug','?')[:40]}: {str(exc)[:60]}", file=sys.stderr)
                continue
            if bq is not None:
                out.append(bq)
    return out


def report(rows: list[pm.BasketQuote]) -> dict[str, Any]:
    rows.sort(key=lambda b: b.volume_24h, reverse=True)
    print("=" * 96)
    print("POLYMARKET NEGRISK BASKET-CONSISTENCY SCREEN  (Yes legs must sum to $1)")
    print("=" * 96)
    hdr = (f"{'out':>4}{'vol24h':>12}{'Σask':>8}{'Σbid':>8}"
           f"{'buyEdge':>9}{'sellEdge':>9}{'askDep':>8}{'bidDep':>8}  field")
    print(hdr)
    print("-" * 96)
    buy_arbs, sell_arbs = [], []
    for b in rows:
        flag = ""
        if b.buy_edge > ARB_THRESHOLD:
            buy_arbs.append(b)
            flag = "  <== BUY ARB"
        elif b.sell_edge > ARB_THRESHOLD:
            sell_arbs.append(b)
            flag = "  <== SELL ARB"
        print(f"{b.n_outcomes:>4}{b.volume_24h:>12,.0f}{b.sum_ask:>8.3f}{b.sum_bid:>8.3f}"
              f"{b.buy_edge:>+9.4f}{b.sell_edge:>+9.4f}{b.min_ask_size:>8,.0f}"
              f"{b.min_bid_size:>8,.0f}  {b.slug[:34]}{flag}")
    if rows:
        print("-" * 96)
        med_a = statistics.median(b.sum_ask for b in rows)
        med_b = statistics.median(b.sum_bid for b in rows)
        print(f"n={len(rows)} fields | median Σask {med_a:.4f} | median Σbid {med_b:.4f} | "
              f"buy-arbs {len(buy_arbs)} | sell-arbs {len(sell_arbs)} (thr {ARB_THRESHOLD:.3f})")
        verdict = (
            "SEALED — no executable basket arb; complement held tight (efficient, fee-free "
            "twin of the Kalshi threshold-arb null)."
            if not buy_arbs and not sell_arbs
            else f"CANDIDATE — {len(buy_arbs)+len(sell_arbs)} field(s) over threshold; "
            "verify depth-weighted execution + persistence before believing."
        )
        print(f"VERDICT: {verdict}")
    return {
        "n_fields": len(rows),
        "median_sum_ask": statistics.median(b.sum_ask for b in rows) if rows else None,
        "median_sum_bid": statistics.median(b.sum_bid for b in rows) if rows else None,
        "buy_arbs": [b.slug for b in buy_arbs],
        "sell_arbs": [b.slug for b in sell_arbs],
        "fields": [vars(b) for b in rows],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=250, help="top-N events by 24h volume to consider")
    ap.add_argument("--max-outcomes", type=int, default=40, help="skip fields with more legs")
    ap.add_argument("--min-volume", type=float, default=10_000.0, help="min 24h volume ($)")
    ap.add_argument("--json", type=str, default=None, help="also write the full result as JSON")
    args = ap.parse_args()

    rows = scan(limit=args.limit, max_outcomes=args.max_outcomes, min_volume=args.min_volume)
    result = report(rows)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
