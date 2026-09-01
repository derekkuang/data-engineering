"""Explore the NegRisk arb route the HONEST way: measure whether a complement
violation is ever CAPTURABLE at our latency — before believing any of it (READ-ONLY, $0).

The deep-research (`docs/research/polymarket_structural_edge_2026.md`) says the arb is
real but bot-saturated: sub-100ms bots, windows median ~2.7–16s, per-conversion profit
~0.08 USDC. Two walls make it inaccessible to us — latency (we poll at ~1s RTT) and the
offshore on-chain execution venue (close-only for US persons). This logger tests the FIRST
wall empirically: poll the top NegRisk fields as fast as the connection allows, and record
every moment the buy-basket cost `Σask < 1` (a live risk-free window). The decisive read:

- If at ~1s polling we essentially NEVER catch an executable window (edge over the ~1.5c
  taker-fee hurdle), that's direct proof the window closes inside our reaction time — the
  latency wall is real for us, exactly like the Kalshi sub-minute BRTI tick race we
  couldn't capture.
- If we DO catch persistent, fee-clearing windows, THAT is the surprising result worth a
  serious (co-located, execution) follow-up conversation.

Usage::

    uv run python -m strategies.pm_ladder_consistency.arb_persistence --seconds 180
    uv run python -m strategies.pm_ladder_consistency.arb_persistence --seconds 300 --fields 5
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field

from ingestion import polymarket as pm

# Executable hurdle: an arb must clear the 2026 taker fee on the legs you cross. Category
# fees run ~1–1.75c/100 shares (geopolitical fee-free); use 1.5c as the buy-basket hurdle.
FEE_HURDLE = 0.015


@dataclass
class FieldStats:
    slug: str
    n_outcomes: int
    polls: int = 0
    violations: int = 0          # polls with buy_edge > 0 (Σask < 1)
    fee_clearing: int = 0        # polls with buy_edge > FEE_HURDLE
    max_edge: float = -1.0
    max_edge_depth: float = 0.0
    episodes: list[int] = field(default_factory=list)  # consecutive-violation run lengths
    _run: int = 0

    def observe(self, buy_edge: float, min_depth: float) -> None:
        self.polls += 1
        if buy_edge > 0:
            self.violations += 1
            self._run += 1
            if buy_edge > self.max_edge:
                self.max_edge, self.max_edge_depth = buy_edge, min_depth
            if buy_edge > FEE_HURDLE:
                self.fee_clearing += 1
        else:
            if self._run:
                self.episodes.append(self._run)
            self._run = 0

    def close(self) -> None:
        if self._run:
            self.episodes.append(self._run)
        self._run = 0


def run(seconds: int, n_fields: int, poll_gap: float) -> list[FieldStats]:
    with pm.client() as c:
        events = pm.fetch_events(c, limit=120)
        fields = []
        for e in events:
            if not e.get("negRisk"):
                continue
            live = [m for m in (e.get("markets") or []) if not m.get("closed")]
            if 3 <= len(live) <= 12:  # small fields poll fast; the arb lives here anyway
                fields.append(e)
            if len(fields) >= n_fields:
                break
        stats = {e["slug"]: FieldStats(e["slug"], len(e["markets"])) for e in fields}
        tokens = {
            e["slug"]: [t for m in e["markets"]
                        if not m.get("closed") and (t := pm.yes_token(m))]
            for e in fields
        }
        print(f"polling {len(fields)} NegRisk fields for {seconds}s "
              f"(hurdle {FEE_HURDLE:.3f})...\n")
        t_end = time.time() + seconds
        rounds = 0
        while time.time() < t_end:
            for e in fields:
                slug = e["slug"]
                try:
                    books = pm.fetch_books(c, tokens[slug])
                    quotes = [pm.leg_quote(books[t]) for t in tokens[slug] if t in books]
                    qs = [q for q in quotes if q is not None]
                    if len(qs) != len(tokens[slug]) or not qs:
                        stats[slug].observe(0.0, 0.0)  # dead leg -> not a live window
                        continue
                    sum_ask = sum(q.best_ask for q in qs)
                    min_depth = min(q.ask_size for q in qs)
                    stats[slug].observe(1.0 - sum_ask, min_depth)
                except Exception:
                    continue
            rounds += 1
            if rounds % 10 == 0:
                seen = sum(s.violations for s in stats.values())
                clr = sum(s.fee_clearing for s in stats.values())
                print(f"  round {rounds}: {seen} violation-polls, {clr} fee-clearing so far")
            time.sleep(poll_gap)
        for s in stats.values():
            s.close()
        return list(stats.values())


def report(stats: list[FieldStats], seconds: int) -> None:
    print("\n" + "=" * 92)
    print("NEGRISK ARB PERSISTENCE — is a complement window ever CAPTURABLE at our latency?")
    print("=" * 92)
    print(f"{'polls':>6}{'viol':>6}{'feeOK':>6}{'maxEdge':>9}{'@depth':>9}"
          f"{'longestRun':>11}  field")
    print("-" * 92)
    tot_polls = tot_viol = tot_clear = 0
    for s in sorted(stats, key=lambda x: x.max_edge, reverse=True):
        longest = max(s.episodes) if s.episodes else 0
        tot_polls += s.polls
        tot_viol += s.violations
        tot_clear += s.fee_clearing
        print(f"{s.polls:>6}{s.violations:>6}{s.fee_clearing:>6}{s.max_edge:>+9.4f}"
              f"{s.max_edge_depth:>9,.0f}{longest:>11}  {s.slug[:36]}")
    print("-" * 92)
    print(f"{tot_polls} total polls over {seconds}s | {tot_viol} showed a violation "
          f"(Σask<1) | {tot_clear} cleared the {FEE_HURDLE:.3f} fee hurdle")
    if tot_clear == 0:
        print("VERDICT: NO fee-clearing window observed at ~1s polling — the arb closes "
              "inside our reaction time. Latency wall CONFIRMED (like the BRTI tick race).")
    else:
        print(f"VERDICT: {tot_clear} fee-clearing poll(s) seen — UNEXPECTED; inspect "
              "persistence + depth before any co-located follow-up.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=int, default=180, help="how long to poll")
    ap.add_argument("--fields", type=int, default=4, help="how many top NegRisk fields")
    ap.add_argument("--poll-gap", type=float, default=0.0, help="sleep between rounds (s)")
    args = ap.parse_args()
    stats = run(args.seconds, args.fields, args.poll_gap)
    report(stats, args.seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
