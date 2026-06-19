"""Liquidity-provision screen — STAGE 2: flow toxicity via trade markouts (Kalshi).

Stage 1 (ml/lp_market_screen.py) found WHERE the spread-capture prize lives. But a
wide spread has two opposite causes: flow is uninformed (you're paid to provide
liquidity -> benign) or flow is informed (you get picked off -> toxic). This stage
tells them apart by measuring the MARKOUT.

The logic. When a trade prints, a taker aggressed and a maker (the seat we want)
was passive. Sign each trade by the taker:  s = +1 if the taker bought YES (lifted
our ask -> we sold), s = -1 if the taker bought NO (= sold YES -> we bought). Track
the mid after the trade. The maker's P&L on that fill at horizon h is

    maker_pnl = half_spread  -  s * (mid[t+h] - mid[t])  =  half_spread + markout

where  markout = -s * (mid[t+h] - mid[t]).  Averaged over many trades:
  * markout ~ 0  -> takers are noise; the mid doesn't move their way; the maker
    keeps the half-spread.  BENIGN -> quote it.
  * markout << 0 -> takers are informed; the mid moves their way right after they
    trade; adverse selection eats the spread.  TOXIC -> avoid. (This is what sank
    the BTC-15m market-making test.)

Net maker edge per fill ~= half_spread + markout (+ maker rebate, extra). That
single number turns "looks rich" (stage 1) into "is actually profitable to quote".

Mid proxy = the per-minute volume-weighted mean from candlesticks (no historical
order book exists). Trades within `h` minutes of settlement are dropped so the
discrete resolution jump (mid -> 0/1) isn't mistaken for adverse selection; the
slower drift toward the outcome IS real maker cost and is kept.

KXBTC15M is included as a KNOWN-TOXIC control: if the method is sound it should
score clearly toxic, while uninformed retail props should not.

Usage:
    uv run python -m ml.lp_toxicity_screen
    uv run python -m ml.lp_toxicity_screen --series KXRT KXNCAABASEBALL --days 5
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from ingestion.kalshi import KalshiClient

# Stage-1 sweet-spot shortlist + known-toxic control (BTC 15m).
DEFAULT_SERIES = [
    "KXRT",  # Rotten Tomatoes — retail/entertainment (expect benign)
    "KXNCAABASEBALL",
    "KXPGATOP20",
    "KXITFMATCH",  # lower-tier tennis
    "KXUFCLIGHTWEIGHTTITLE",
    "KXWCSTAGEOFELIM",
    "KXTRUMPCOUNTRIES",  # politics
    "KXCPIYOY",  # macro print — expect informed/toxic despite wide spread
    "KXBTC15M",  # CONTROL: known toxic
]
HORIZONS = (1, 5)  # markout horizons, minutes
MARKETS_PER_SERIES = 8
TRADE_PAGES = 6  # x100 trades per market, most-recent first
LOOKBACK_S = 24 * 3600  # bound candle/trade window to the last day of each market


def _f(d: dict[str, Any] | None, key: str) -> float | None:
    v = (d or {}).get(key)
    return float(v) if v is not None else None


def _unix(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def _fetch_trades(client: KalshiClient, ticker: str, since_ts: int) -> list[dict[str, Any]]:
    """Most-recent trades for a market, paginated, stopping once older than since_ts."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(TRADE_PAGES):
        p: dict[str, Any] = {"ticker": ticker, "limit": 100}
        if cursor:
            p["cursor"] = cursor
        resp = client.get("/markets/trades", params=p)
        trades = resp.get("trades", [])
        out.extend(trades)
        if trades and _unix(trades[-1]["created_time"]) < since_ts:
            break
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    return out


def _market_markouts(
    client: KalshiClient, series: str, market: dict[str, Any]
) -> tuple[dict[int, list[float]], list[float]]:
    """Per-horizon markout samples (dollars) + half-spread samples for one market."""
    open_ts, close_ts = _unix(market["open_time"]), _unix(market["close_time"])
    lo = max(open_ts, close_ts - LOOKBACK_S)
    candles = client.get_market_candlesticks(
        series, market["ticker"], lo - 120, close_ts + max(HORIZONS) * 60 + 120, 1
    )
    mid: dict[int, float] = {}
    half_spreads: list[float] = []
    for c in candles:
        m_ts = int(c["end_period_ts"]) - 60
        price = c.get("price") or {}
        mp = price.get("mean_dollars") or price.get("close_dollars")
        if mp is not None:
            mid[m_ts] = float(mp)
        yb, ya = _f(c.get("yes_bid"), "close_dollars"), _f(c.get("yes_ask"), "close_dollars")
        if yb is not None and ya is not None and ya > yb:
            half_spreads.append((ya - yb) / 2.0)

    # Forward-fill the mid over the minute grid: candles exist only for ACTIVE
    # minutes, but a quiet minute just means the price didn't move (a legitimate
    # ~0 markout, not a missing sample). Without this, sparse markets — exactly
    # the wide-spread retail targets — get dropped for too few samples.
    if mid:
        grid_lo, last = min(mid), None
        for ts in range(grid_lo, close_ts + 60, 60):
            if ts in mid:
                last = mid[ts]
            elif last is not None:
                mid[ts] = last

    samples: dict[int, list[float]] = {h: [] for h in HORIZONS}
    for t in _fetch_trades(client, market["ticker"], lo):
        ct = _unix(t["created_time"])
        if ct < lo:
            continue
        t_min = (ct // 60) * 60
        s = 1.0 if t.get("taker_side") == "yes" else -1.0
        m0 = mid.get(t_min)
        if m0 is None:
            continue
        for h in HORIZONS:
            if t_min + h * 60 > close_ts - 60:  # drop near-resolution (avoid the 0/1 jump)
                continue
            mh = mid.get(t_min + h * 60)
            if mh is not None:
                samples[h].append(-s * (mh - m0))
    return samples, half_spreads


def screen_series(client: KalshiClient, series: str, days: int) -> dict[str, Any] | None:
    since = datetime.now(UTC) - timedelta(days=days)
    markets = client.list_markets(
        series, status="settled", min_close_ts=int(since.timestamp()), page_limit=200
    )
    markets = [m for m in markets if m.get("result") in ("yes", "no")]
    markets.sort(key=lambda m: m.get("volume_fp") or 0.0, reverse=True)
    markets = markets[:MARKETS_PER_SERIES]
    if not markets:
        return None

    all_samples: dict[int, list[float]] = {h: [] for h in HORIZONS}
    half_spreads: list[float] = []
    for m in markets:
        s, hs = _market_markouts(client, series, m)
        for h in HORIZONS:
            all_samples[h].extend(s[h])
        half_spreads.extend(hs)

    n = len(all_samples[HORIZONS[0]])
    if n < 30 or not half_spreads:
        return None
    half = statistics.median(half_spreads) * 100.0  # cents
    out: dict[str, Any] = {"series": series, "n_trades": n, "half_spread_c": half}
    for h in HORIZONS:
        mk = statistics.mean(all_samples[h]) * 100.0 if all_samples[h] else float("nan")
        out[f"markout_{h}c"] = mk
        out[f"net_{h}c"] = half + mk
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=DEFAULT_SERIES)
    ap.add_argument("--days", type=int, default=7, help="settled-market lookback")
    args = ap.parse_args()

    client = KalshiClient(pace_seconds=0.15)
    rows: list[dict[str, Any]] = []
    for s in args.series:
        print(f"  screening {s} ...")
        r = screen_series(client, s, args.days)
        if r:
            rows.append(r)
    client.close()
    if not rows:
        print("No series with enough trades.")
        return 1

    def _net5(r: dict[str, Any]) -> float:
        v = r["net_5c"]
        return float(v) if v == v else float(r["net_1c"])  # fall back to 1m if 5m is nan

    rows.sort(key=_net5, reverse=True)
    print("\n" + "=" * 86)
    print("STAGE 2 — FLOW TOXICITY (trade markouts).  net = half_spread + markout, per fill")
    print("=" * 86)
    print(
        f"{'series':<24}{'trades':>8}{'half_spr':>10}{'mkout@1m':>10}"
        f"{'mkout@5m':>10}{'net@1m':>9}{'net@5m':>9}  verdict"
    )
    print("-" * 86)
    for r in rows:
        net5 = _net5(r)
        verdict = "QUOTE" if net5 > 0.2 else ("marginal" if net5 > -0.2 else "TOXIC")
        print(
            f"{r['series'][:23]:<24}{r['n_trades']:>8,}{r['half_spread_c']:>9.2f}c"
            f"{r['markout_1c']:>+9.2f}c{r['markout_5c']:>+9.2f}c"
            f"{r['net_1c']:>+8.2f}c{net5:>+8.2f}c  {verdict}"
        )

    print("\nRead: markout < 0 = the mid moves the taker's way after they trade = adverse")
    print("selection (toxic). net@h = half_spread + markout@h = the maker's edge per fill")
    print("before rebates (Kalshi maker rebate adds up to ~0.5c at mid). QUOTE = the spread")
    print("survives the toxicity; TOXIC = informed flow eats it. KXBTC15M is the control —")
    print("it should land clearly toxic; if a retail prop lands QUOTE, that's a real target.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
