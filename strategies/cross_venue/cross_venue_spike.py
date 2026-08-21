"""Cross-venue feasibility spike: Kalshi KXBTC15M vs Polymarket BTC 15m Up/Down.

Research question (artifact, NOT execution). Both venues run the "same" event —
"is BTC higher 15 minutes from now?" — on clock-aligned 15-min windows. But they
settle it differently:

  * Kalshi KXBTC15M: "Up" if the 60-second average of CF Benchmarks' BTC
    Real-Time Index at window CLOSE >= the 60-second average at window OPEN.
  * Polymarket 15m: "Up" if the Chainlink BTC/USD data-stream price at the end
    of the window >= the price at the start (point-to-point; the contract text
    explicitly says "not according to other sources or spot markets").

Different reference index AND different settlement math (TWAP vs point read). So
the naive cross-venue "arb" — buy YES on the cheaper venue, NO on the dearer —
is only risk-free if the two venues ALWAYS resolve the same way. This script
measures how often they actually agree, over matched windows pulled from both
PUBLIC APIs (no auth, no live collector — settled history only), and buckets the
disagreement by the size of the BTC move (the mismatch should concentrate in
near-coin-flip windows, which is exactly where the binary volume sits).

Outputs: coverage, the overall outcome-disagreement rate with a day-block
bootstrap CI, and disagreement vs |move|. A disagreement rate materially above 0
means "risk-free arb" is the wrong frame — it is statistical arb with basis risk
that has to clear fees + the disagreement-loss tax.

Usage:
    uv run python -m strategies.cross_venue.cross_venue_spike --days 9
    uv run python -m strategies.cross_venue.cross_venue_spike --start 2026-06-04 --end 2026-06-12
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ingestion.coinbase import fetch_bars
from ingestion.kalshi import SERIES_BTC_15M, KalshiClient

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
GAMMA_BATCH = 20  # slugs per multi-slug request
GAMMA_PACE_S = 1.1  # public endpoint limit is ~60 req/min
WINDOW_SECONDS = 900


@dataclass
class Window:
    """One 15-min window matched across both venues."""

    open_ts: int  # window start, Unix seconds (the join key; == Polymarket slug ts)
    kalshi_up: bool
    poly_up: bool
    move_usd: float | None = None  # BTC point-to-point move over the window (Coinbase proxy)


# --- Kalshi -----------------------------------------------------------------
def fetch_kalshi_outcomes(client: KalshiClient, start: datetime, end: datetime) -> dict[int, bool]:
    """{window_open_ts -> kalshi_up} for settled KXBTC15M markets in [start, end)."""
    markets = client.list_markets(
        SERIES_BTC_15M,
        status="settled",
        min_close_ts=int(start.timestamp()),
        max_close_ts=int(end.timestamp()),
    )
    out: dict[int, bool] = {}
    for m in markets:
        result = m.get("result")
        if result not in ("yes", "no"):
            continue  # voided / unsettled — drop
        open_ts = int(datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
        out[open_ts] = result == "yes"  # KXBTC15M "yes" == BTC up
    return out


# --- Polymarket -------------------------------------------------------------
def _parse_poly_outcome(event: dict[str, Any]) -> bool | None:
    """True=Up, False=Down, None if not cleanly resolved."""
    if not event.get("closed"):
        return None
    market = (event.get("markets") or [{}])[0]
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")
    if isinstance(outcomes, str):
        outcomes, prices = json.loads(outcomes), json.loads(prices or "[]")
    if outcomes != ["Up", "Down"] or len(prices) != 2:
        return None
    if prices[0] == "1" and prices[1] == "0":
        return True
    if prices[0] == "0" and prices[1] == "1":
        return False
    return None  # ambiguous (e.g. 50/50 void)


def fetch_polymarket_outcomes(open_tss: list[int]) -> dict[int, bool]:
    """{window_open_ts -> poly_up} via batched Gamma multi-slug fetches."""
    out: dict[int, bool] = {}
    with httpx.Client(timeout=30.0) as http:
        for i in range(0, len(open_tss), GAMMA_BATCH):
            chunk = open_tss[i : i + GAMMA_BATCH]
            params = httpx.QueryParams([("slug", f"btc-updown-15m-{ts}") for ts in chunk])
            resp = http.get(f"{GAMMA_BASE}/events", params=params)
            resp.raise_for_status()
            for event in resp.json():
                slug = event.get("slug", "")
                try:
                    ts = int(slug.rsplit("-", 1)[1])
                except (IndexError, ValueError):
                    continue
                outcome = _parse_poly_outcome(event)
                if outcome is not None:
                    out[ts] = outcome
            time.sleep(GAMMA_PACE_S)
    return out


# --- BTC move (Coinbase proxy for bucketing) --------------------------------
def fetch_btc_move(start: datetime, end: datetime) -> dict[int, float]:
    """{minute_ts -> BTC open price} so move = price[end] - price[start] per window."""
    bars = fetch_bars("BTC-USD", start - timedelta(minutes=2), end + timedelta(minutes=20))
    return {int(b.event_at.timestamp()): b.open for b in bars}


# --- stats ------------------------------------------------------------------
def day_block_bootstrap(
    windows: list[Window], n_boot: int = 5000, seed: int = 0
) -> tuple[float, float]:
    """95% CI on the disagreement rate, resampling whole DAYS with replacement
    (disagreements cluster by regime, so the effective sample is days, not windows)."""
    by_day: dict[str, list[Window]] = defaultdict(list)
    for w in windows:
        day = datetime.fromtimestamp(w.open_ts, UTC).strftime("%Y-%m-%d")
        by_day[day].append(w)
    days = list(by_day.values())
    rng = random.Random(seed)
    rates: list[float] = []
    for _ in range(n_boot):
        sample = [w for _ in days for w in rng.choice(days)]
        disagree = sum(w.kalshi_up != w.poly_up for w in sample)
        rates.append(disagree / len(sample))
    rates.sort()
    return rates[int(0.025 * n_boot)], rates[int(0.975 * n_boot)]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=str, default=None, help="UTC date YYYY-MM-DD")
    ap.add_argument("--end", type=str, default=None, help="UTC date YYYY-MM-DD (exclusive-ish)")
    ap.add_argument("--days", type=int, default=9, help="lookback if --start/--end omitted")
    args = ap.parse_args()

    if args.start and args.end:
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    else:
        end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        start = end - timedelta(days=args.days)

    print(f"Window: {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC\n")

    client = KalshiClient()
    print("Fetching Kalshi settled outcomes ...")
    kalshi = fetch_kalshi_outcomes(client, start, end)
    client.close()
    print(f"  Kalshi settled windows: {len(kalshi)}")

    print("Fetching Polymarket settled outcomes ...")
    poly = fetch_polymarket_outcomes(sorted(kalshi))
    print(f"  Polymarket settled windows: {len(poly)}")

    print("Fetching BTC minute bars (Coinbase, for move bucketing) ...")
    px = fetch_btc_move(start, end)

    matched = sorted(set(kalshi) & set(poly))
    windows: list[Window] = []
    for ts in matched:
        move = None
        if ts in px and (ts + WINDOW_SECONDS) in px:
            move = px[ts + WINDOW_SECONDS] - px[ts]
        windows.append(Window(ts, kalshi[ts], poly[ts], move))

    n = len(windows)
    if n == 0:
        print("\nNo matched windows — check date range / coverage.")
        return

    disagree = [w for w in windows if w.kalshi_up != w.poly_up]
    rate = len(disagree) / n
    lo, hi = day_block_bootstrap(windows)
    days = len({datetime.fromtimestamp(w.open_ts, UTC).strftime("%Y-%m-%d") for w in windows})

    print("\n" + "=" * 64)
    print("CROSS-VENUE OUTCOME AGREEMENT — Kalshi KXBTC15M vs Polymarket 15m")
    print("=" * 64)
    print(f"Matched windows : {n}  over {days} days")
    print(f"Coverage        : Kalshi {len(kalshi)} / Polymarket {len(poly)} / matched {n}")
    print(f"Agree           : {n - len(disagree)}  ({100 * (1 - rate):.1f}%)")
    print(
        f"DISAGREE        : {len(disagree)}  ({100 * rate:.2f}%)  "
        f"[day-block 95% CI {100 * lo:.2f}% - {100 * hi:.2f}%]"
    )

    # disagreement vs |move|: the mismatch should concentrate near a coin flip
    with_move = [w for w in windows if w.move_usd is not None]
    if with_move:
        print("\n|BTC move over window| (Coinbase proxy)   windows   disagree-rate")
        edges = [0, 5, 15, 40, 100, 1e9]
        labels = ["< $5", "$5-15", "$15-40", "$40-100", "> $100"]
        for label, lo_e, hi_e in zip(labels, edges[:-1], edges[1:], strict=True):
            bucket = [
                w for w in with_move if w.move_usd is not None and lo_e <= abs(w.move_usd) < hi_e
            ]
            if bucket:
                br = sum(w.kalshi_up != w.poly_up for w in bucket) / len(bucket)
                print(f"  {label:<10}{'':>22}{len(bucket):>7}   {100 * br:>6.1f}%")

    print("\nInterpretation:")
    print("  Risk-free cross-venue arb requires a ~0% disagreement rate. Any")
    print("  material rate means YES-here/NO-there can lose BOTH legs, so this")
    print("  is statistical arb with basis risk, not a locked profit. The edge")
    print("  must clear Kalshi fees (~0.7%) + Polymarket fees + the disagreement")
    print("  tax (P(disagree) x ~stake). Bucketing shows where the risk lives.")


if __name__ == "__main__":
    main()
