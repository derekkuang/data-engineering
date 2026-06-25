"""Liquidity-provision market screen — STAGE 1: the opportunity landscape (Kalshi).

The goal of the LP business is to earn, per filled contract,

    half_spread + maker_rebate  -  adverse_selection  -  inventory_cost  >  0

Kalshi pays makers (zero maker fee + rebates), so the gross prize is the spread
that actually changes hands; the killer is adverse selection (informed takers
lift your quote right before the price moves against you — exactly what sank the
BTC-15m market-making test, where the book is too efficient for the half-spread
to cover the move).

This screen has two stages:
  * STAGE 1 (this file): map the OPPORTUNITY across the whole open universe from
    cheap market snapshots — where does `volume x spread` actually live, by
    category and series? This needs no price history, just the live market list.
  * STAGE 2 (separate): for the top candidates, measure TOXICITY — does flow
    predict price (markouts) and does the half-spread cover the short-horizon
    volatility. That discounts the gross prize down to a net maker edge.

Two metrics per series/category, both from the public /markets snapshot:
  * spread_capture  = sum over markets of (volume_24h * spread/2)
      The gross half-spread dollars a maker could in principle capture per day
      (an upper bound — you won't win every fill — but a sound RELATIVE ranking).
  * median_spread (cents) = the per-fill richness / competition proxy. A liquid
      market with a 1c spread is crowded and efficient (little to earn, likely
      toxic); a market with a wide spread AND real volume is the sweet spot.

The contrast is the insight: max-volume / tiny-spread corners (e.g. BTC 15m) are
crowded and efficient; moderate-volume / wide-spread corners are where an
attentive maker is paid to provide liquidity. Stage 2 then checks toxicity.

Usage:
    uv run python -m ml.lp.lp_market_screen                 # full open universe
    uv run python -m ml.lp.lp_market_screen --top 25        # show more series
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ingestion.kalshi import KalshiClient

EVENTS_PAGE_LIMIT = 200
MAX_PAGES = 200  # safety cap on pagination
MIN_VOLUME_24H = 1.0  # ignore markets with no flow — nothing to capture


@dataclass
class Row:
    ticker: str
    series: str
    category: str
    spread: float  # dollars (yes_ask - yes_bid)
    mid: float
    volume_24h: float
    open_interest: float
    liquidity: float

    @property
    def capture(self) -> float:
        """Gross maker half-spread dollars/day if you caught all the volume."""
        return self.volume_24h * self.spread / 2.0


def _f(d: dict[str, Any], key: str) -> float | None:
    v = d.get(key)
    return float(v) if v is not None else None


def fetch_open_events(client: KalshiClient) -> list[dict[str, Any]]:
    """Paginate open events WITH nested markets. Events carry series_ticker +
    category and exclude the auto-generated MVE parlay markets that flood the
    raw /markets feed, so this is the clean entry point to the real universe."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        p: dict[str, Any] = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": EVENTS_PAGE_LIMIT,
        }
        if cursor:
            p["cursor"] = cursor
        resp = client.get("/events", params=p)
        out.extend(resp.get("events", []))
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    return out


def fetch_rows(client: KalshiClient) -> list[Row]:
    """One Row per open market with a two-sided quote and real 24h volume,
    tagged with its event's series + category."""
    rows: list[Row] = []
    for e in fetch_open_events(client):
        series = e.get("series_ticker") or e.get("event_ticker", "").split("-")[0]
        category = e.get("category") or "Unknown"
        for m in e.get("markets") or []:
            bid, ask = _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars")
            vol = _f(m, "volume_24h_fp") or 0.0
            if bid is None or ask is None or ask <= bid or vol < MIN_VOLUME_24H:
                continue
            rows.append(
                Row(
                    ticker=m["ticker"],
                    series=series,
                    category=category,
                    spread=ask - bid,
                    mid=(ask + bid) / 2.0,
                    volume_24h=vol,
                    open_interest=_f(m, "open_interest_fp") or 0.0,
                    liquidity=_f(m, "liquidity_dollars") or 0.0,
                )
            )
    return rows


def _agg(rows: list[Row]) -> dict[str, float]:
    return {
        "n_markets": float(len(rows)),
        "capture": sum(r.capture for r in rows),
        "volume_24h": sum(r.volume_24h for r in rows),
        "median_spread_c": statistics.median(r.spread for r in rows) * 100.0,
        "liquidity": sum(r.liquidity for r in rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="series to show")
    args = ap.parse_args()

    client = KalshiClient(pace_seconds=0.2)
    print("Fetching open events with nested markets (paginating the universe) ...")
    rows = fetch_rows(client)
    client.close()
    print(f"  {len(rows)} open markets with a two-sided quote + 24h volume\n")
    if not rows:
        print("No qualifying markets.")
        return 1

    total_capture = sum(r.capture for r in rows)

    # --- by category -------------------------------------------------------
    by_cat: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_cat[r.category].append(r)
    cat_rows = sorted(by_cat.items(), key=lambda kv: _agg(kv[1])["capture"], reverse=True)

    print("=" * 84)
    print("STAGE 1 — OPPORTUNITY LANDSCAPE BY CATEGORY")
    print("=" * 84)
    print(
        f"{'category':<20}{'mkts':>7}{'24h volume':>14}{'med spread':>12}"
        f"{'capture $/day':>15}{'share':>8}"
    )
    print("-" * 84)
    for cat, rs in cat_rows:
        a = _agg(rs)
        share = a["capture"] / total_capture if total_capture else 0.0
        print(
            f"{cat[:19]:<20}{int(a['n_markets']):>7}{a['volume_24h']:>14,.0f}"
            f"{a['median_spread_c']:>11.1f}c{a['capture']:>14,.0f}{share:>8.1%}"
        )

    # --- top series --------------------------------------------------------
    by_series: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_series[r.series].append(r)
    series_ranked = sorted(by_series.items(), key=lambda kv: _agg(kv[1])["capture"], reverse=True)

    print("\n" + "=" * 84)
    print(f"TOP {args.top} SERIES BY GROSS SPREAD-CAPTURE  (the maker prize, pre-toxicity)")
    print("=" * 84)
    print(
        f"{'series':<22}{'category':<14}{'mkts':>5}{'24h vol':>11}"
        f"{'med spr':>9}{'capture $/day':>15}"
    )
    print("-" * 84)
    for series, rs in series_ranked[: args.top]:
        a = _agg(rs)
        print(
            f"{series[:21]:<22}{rs[0].category[:13]:<14}{int(a['n_markets']):>5}"
            f"{a['volume_24h']:>11,.0f}{a['median_spread_c']:>8.1f}c{a['capture']:>14,.0f}"
        )

    # --- the SWEET SPOT: moderate spread + steady flow --------------------
    # The actual LP target band: a spread WIDE enough to earn per fill but not so
    # wide it's a broken/one-sided book (>~25c usually = a token 1c-vs-99c quote,
    # no real two-sided market — a trap, not an opportunity), with real volume so
    # there are fills to capture. Ranked by gross capture = the stage-2 shortlist.
    SPREAD_LO, SPREAD_HI, MIN_VOL = 3.0, 25.0, 5000.0
    sweet: list[tuple[str, dict[str, float]]] = []
    for s, rs in by_series.items():
        a = _agg(rs)
        if a["volume_24h"] >= MIN_VOL and SPREAD_LO <= a["median_spread_c"] <= SPREAD_HI:
            sweet.append((s, a))
    sweet.sort(key=lambda kv: kv[1]["capture"], reverse=True)
    print("\n" + "=" * 84)
    print(
        f"SWEET SPOT — spread {SPREAD_LO:.0f}-{SPREAD_HI:.0f}c AND >={MIN_VOL:,.0f} contracts/24h"
    )
    print("(earn-per-fill without a broken book + real flow to capture -> stage-2 targets)")
    print("=" * 84)
    print(f"{'series':<24}{'category':<16}{'24h vol':>11}{'med spread':>13}{'capture $/day':>15}")
    print("-" * 84)
    for series, a in sweet[: args.top]:
        cat = by_series[series][0].category
        print(
            f"{series[:23]:<24}{cat[:15]:<16}{a['volume_24h']:>11,.0f}"
            f"{a['median_spread_c']:>11.1f}c{a['capture']:>14,.0f}"
        )

    print("\nRead: 'capture $/day' is the GROSS half-spread prize (upper bound — assumes")
    print("you win every fill). The tight-spread / huge-volume giants (World Cup, tennis,")
    print("BTC) top the raw capture list but are crowded + efficient = likely toxic. The")
    print("SWEET SPOT band is where a maker earns real $/fill with genuine two-sided flow")
    print("-- Stage 2 measures toxicity (flow->price markouts, half-spread vs volatility)")
    print("on these to find the ones whose spread is wide because flow is UNINFORMED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
