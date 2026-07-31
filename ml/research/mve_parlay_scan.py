"""Is there a tradeable edge in fading the retail longshot-parlay bias on Kalshi?

MVE (Multivariate Event) contracts are Kalshi's parlay product, live since Dec-2025:
one binary that pays YES iff EVERY selected leg settles to its chosen side. Each
combo carries a machine-readable ``mve_selected_legs`` list, so we can price the
parlay against its own legs. The hypothesis worth testing is the one documented
behavioral inefficiency the rest of the alpha hunt didn't cover: retail systematically
OVERPAYS for parlays (the lottery-payout appeal + correlation neglect), so the combo
should price ABOVE the leg-implied joint probability — an overpricing you could fade.

This is a desk study, not a bot: market-internal, public data, one script (the
``altcoin_efficiency`` / ``favorite_longshot`` pattern). It answers two questions
in order, because the second only matters if the first passes:

  PART A — the tradeability GATE. Of the whole open MVE universe, what actually has
    a two-sided quote AND real participation? The fade needs a side to SELL the
    overpriced parlay from. If the traded combos are one-sided (buy-only) and the
    two-sided combos never trade, there is no fadeable surface — the bias can be
    real and still structurally uncapturable (the "trapped inside the spread" death,
    in its most extreme form: no bid at all).

  PART B — the BIAS magnitude. On combos we CAN price (all legs still active with a
    two-sided quote), how far does the combo ask sit above the independent-joint
    fair value? This quantifies the overpricing whether or not it's tradeable —
    turning a liquidity null into a measured "the bias is real and this big, here's
    why you can't take it." Cross-game combos (legs from distinct games ~ independent)
    are reported separately as the cleanest estimate; same-game legs are correlated,
    which biases the naive product and is flagged, not trusted.

Leg pricing: P(leg hits its side) = leg yes-mid (side=yes) or 1 - yes-mid (side=no);
independent joint = product over legs; overround = combo_ask - joint (what retail
pays minus fair). Positive & large = the bias.

Usage:
    uv run python -m ml.research.mve_parlay_scan                    # default 40 pages
    uv run python -m ml.research.mve_parlay_scan --pages 90 --sample 600
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import KalshiClient

PAGE_LIMIT = 1000
LEG_BATCH = 100  # /markets?tickers= accepts up to 100 comma-separated tickers


def _f(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _game_token(event_ticker: str) -> str:
    """The date+matchup suffix shared by every market on the same game, e.g.
    'KXMLBTOTAL-26JUL281940NYYCWS' -> '26JUL281940NYYCWS'. Two legs with the same
    token are the same game (correlated); distinct tokens are ~independent."""
    parts = event_ticker.split("-", 1)
    return parts[1] if len(parts) > 1 else event_ticker


@dataclass
class MveMarket:
    ticker: str
    collection: str
    n_legs: int
    yes_bid: float | None
    yes_ask: float | None
    life_vol: float
    oi: float
    provisional: bool
    legs: list[dict[str, str]]  # [{event_ticker, market_ticker, side}, ...]

    @property
    def two_sided(self) -> bool:
        return (
            self.yes_bid is not None
            and self.yes_ask is not None
            and self.yes_ask > self.yes_bid
            and self.yes_bid > 0
        )

    @property
    def traded(self) -> bool:
        return self.life_vol > 0 or self.oi > 0

    @property
    def cross_game(self) -> bool:
        toks = {_game_token(leg["event_ticker"]) for leg in self.legs}
        return len(toks) == self.n_legs


def fetch_mve_universe(client: KalshiClient, max_pages: int) -> list[MveMarket]:
    """Paginate the open /markets feed and keep every market carrying selected legs.
    (MVE combos are excluded from the /events path the rest of the pipeline uses, so
    the raw /markets feed — which they flood — is the only way to see them.)"""
    out: list[MveMarket] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"status": "open", "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/markets", params=params)
        for m in resp.get("markets", []):
            legs = m.get("mve_selected_legs")
            if not legs:
                continue
            out.append(
                MveMarket(
                    ticker=m["ticker"],
                    collection=m.get("mve_collection_ticker") or "?",
                    n_legs=len(legs),
                    yes_bid=_f(m, "yes_bid_dollars"),
                    yes_ask=_f(m, "yes_ask_dollars"),
                    life_vol=_f(m, "volume_fp") or 0.0,
                    oi=_f(m, "open_interest_fp") or 0.0,
                    provisional=bool(m.get("is_provisional")),
                    legs=legs,
                )
            )
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    return out


def report_gate(mve: list[MveMarket]) -> None:
    """PART A — the structure of the book: is there any fadeable surface?"""
    n = len(mve)
    prov = sum(m.provisional for m in mve)
    traded = [m for m in mve if m.traded]
    two_sided = [m for m in mve if m.two_sided]
    both = [m for m in mve if m.traded and m.two_sided]
    legs = [m.n_legs for m in mve]

    print("=" * 78)
    print("PART A — TRADEABILITY GATE  (is there a side to fade from?)")
    print("=" * 78)
    print(f"open MVE combos scanned:        {n:>8,}")
    print(f"  provisional (auto-shell):     {prov:>8,}  ({prov / n:.1%})")
    print(f"  traded (lifetime vol or OI):  {len(traded):>8,}  ({len(traded) / n:.2%})")
    print(f"  two-sided quote (bid>0<ask):  {len(two_sided):>8,}  ({len(two_sided) / n:.2%})")
    print(f"  BOTH traded AND two-sided:    {len(both):>8,}  ({len(both) / n:.3%}) fadeable set")
    if legs:
        print(f"  legs/combo: min {min(legs)} median {statistics.median(legs):.0f} max {max(legs)}")

    by_coll: Counter[str] = Counter(m.collection for m in mve)
    print("\ncollections:")
    for coll, cnt in by_coll.most_common(6):
        lv = sum(m.life_vol for m in mve if m.collection == coll)
        print(f"  {cnt:>7,} combos   lifetime_vol {lv:>12,.0f}   {coll}")

    # The crux: among combos that actually traded, is there a resting bid to sell into?
    if traded:
        with_bid = sum(1 for m in traded if (m.yes_bid or 0) > 0)
        print(
            f"\ntraded combos with a live YES bid (a side to sell into): "
            f"{with_bid}/{len(traded)}  ({with_bid / len(traded):.2%})"
        )
    print(
        "\nRead: retail buys parlays and HOLDS to expiry (vol ~= OI), so the traded\n"
        "combos are one-sided (buy-only, no resting bid) while the two-sided combos\n"
        "are the ones nobody trades. If the fadeable set above is ~0, the overpricing\n"
        "measured in Part B is real but has NO side to take it from."
    )


@dataclass
class BiasRow:
    ticker: str
    n_legs: int
    combo_ask: float
    joint: float
    cross_game: bool
    overround_c: float = field(init=False)

    def __post_init__(self) -> None:
        self.overround_c = (self.combo_ask - self.joint) * 100.0


def price_legs(client: KalshiClient, tickers: list[str]) -> dict[str, float | None]:
    """Current yes-mid per leg market (None if unpriceable / one-sided / not active)."""
    mids: dict[str, float | None] = {}
    uniq = sorted(set(tickers))
    for i in range(0, len(uniq), LEG_BATCH):
        chunk = uniq[i : i + LEG_BATCH]
        resp = client.get("/markets", params={"tickers": ",".join(chunk)})
        for m in resp.get("markets", []):
            yb, ya = _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars")
            active = m.get("status") == "active"
            if active and yb is not None and ya is not None and ya > yb:
                mids[m["ticker"]] = (yb + ya) / 2.0
            else:
                mids[m["ticker"]] = None
    return mids


def measure_bias(client: KalshiClient, mve: list[MveMarket], max_legs: int, sample: int) -> None:
    """PART B — how far above the independent-joint fair value does the combo ask sit?"""
    # Candidates: a real buy price (0<ask<1) and few enough legs to be a retail parlay.
    cand = [
        m
        for m in mve
        if m.yes_ask is not None
        and 0.0 < m.yes_ask < 1.0
        and 2 <= m.n_legs <= max_legs
    ]
    n_priceable = len(cand)
    if n_priceable > sample:
        # RANDOM (not head-of-feed) slice so the estimate isn't biased by /markets pagination
        # order; seeded for reproducibility. Report how many were dropped (no silent cap).
        cand = random.Random(0).sample(cand, sample)
    leg_tickers = [leg.get("market_ticker", "") for m in cand for leg in m.legs]
    print("\n" + "=" * 78)
    print("PART B — BIAS MAGNITUDE  (combo ask vs leg-implied independent joint)")
    print("=" * 78)
    note = f"  (random sample of {n_priceable} priceable)" if n_priceable > sample else ""
    print(f"candidate combos (<= {max_legs} legs, live ask): {len(cand)}{note}")
    print(f"pricing {len(set(leg_tickers))} distinct legs ...")
    mids = price_legs(client, leg_tickers)

    rows: list[BiasRow] = []
    skipped = 0
    for m in cand:
        joint = 1.0
        ok = True
        for leg in m.legs:
            mid = mids.get(leg.get("market_ticker", ""))
            side = leg.get("side")
            if mid is None or side not in ("yes", "no"):  # unpriceable, or unknown side -> skip
                ok = False
                break
            p_side = mid if side == "yes" else 1.0 - mid
            joint *= p_side
        if not ok or joint <= 0.0 or m.yes_ask is None:
            skipped += 1
            continue
        rows.append(BiasRow(m.ticker, m.n_legs, m.yes_ask, joint, m.cross_game))

    print(f"priced: {len(rows)}   skipped (a leg unpriceable/settled): {skipped}")
    if not rows:
        print("no priceable combos — cannot measure the bias this run.")
        return

    def summarize(label: str, rs: list[BiasRow]) -> None:
        if not rs:
            print(f"\n  {label}: (none)")
            return
        ov = sorted(r.overround_c for r in rs)
        frac_pos = sum(1 for x in ov if x > 0) / len(ov)
        med = statistics.median(ov)
        p25, p75 = ov[len(ov) // 4], ov[(3 * len(ov)) // 4]
        # overround as % of fair value, at the median
        med_ratio = statistics.median(r.combo_ask / r.joint for r in rs)
        print(f"\n  {label}  (n={len(rs)})")
        print(f"    combo ask ABOVE joint: {frac_pos:.1%} of combos")
        print(f"    overround (combo_ask - joint): median {med:+.2f}c IQR [{p25:+.2f},{p75:+.2f}]c")
        print(f"    combo ask / fair joint: median {med_ratio:.2f}x")

    summarize("ALL priceable", rows)
    summarize("CROSS-GAME only (legs ~independent, cleanest)", [r for r in rows if r.cross_game])
    summarize("SAME-GAME present (correlated -> product biased, caveat)",
              [r for r in rows if not r.cross_game])
    print(
        "\nRead: a positive overround = retail pays ABOVE the leg-implied fair value for\n"
        "the parlay (the documented longshot-parlay bias / Kalshi's parlay vig). Positive\n"
        "correlation among same-game legs would RAISE the true joint, so the cross-game\n"
        "number is the trustworthy one; same-game overstates the gap. Whatever its size,\n"
        "Part A decides whether any of it is reachable."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Kalshi MVE parlay overpricing desk scan")
    ap.add_argument("--pages", type=int, default=40, help="open /markets pages (1000/pg)")
    ap.add_argument("--max-legs", type=int, default=6, help="cap legs/combo for the bias cohort")
    ap.add_argument("--sample", type=int, default=400, help="max candidate combos priced in Part B")
    args = ap.parse_args()

    load_dotenv()
    client = KalshiClient(pace_seconds=0.15)
    try:
        print(f"Fetching up to {args.pages} pages of the open MVE universe ...")
        mve = fetch_mve_universe(client, args.pages)
        if not mve:
            print("No MVE markets found.")
            return 1
        report_gate(mve)
        measure_bias(client, mve, args.max_legs, args.sample)
    finally:
        client.close()

    print("\n" + "=" * 78)
    print("VERDICT: the fade needs a two-sided, participated market. If Part A's")
    print("fadeable set is ~0 and Part B's overround is large, the bias is REAL but")
    print("STRUCTURALLY UNCAPTURABLE — the book is buy-only where the volume is.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
