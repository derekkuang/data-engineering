"""Read-only MULTI-MARKET WebSocket feed (Phase 3) — watch the whole active board at once.

This is the scaling unlock: instead of polling ONE market over REST (~5 calls/cycle, which
capped the maker at a single market), this subscribes to every active eligible market on one
WebSocket connection and maintains a real-time local book for each (via ingestion.kalshi_ws).
It places NO orders — its jobs are (1) prove the multi-market feed is correct at scale (a
reconciler compares each local book to a fresh REST snapshot; seq-gap counting is per-stream)
and (2) capture many markets concurrently for analysis. The live multi-market MAKER (Phase 5)
reuses this exact feed.

Market selection reuses the maker's gate (active TOTAL/SPREAD, prefix-filterable); the set
rolls as games start/end via update_subscription (no reconnect).

Usage:
    uv run python -m ml.lp.ws_logger --minutes 30                 # active sports board
    uv run python -m ml.lp.ws_logger --prefix KXWC --minutes 30   # World-Cup-only
    uv run python -m ml.lp.ws_logger --btc --minutes 5            # smoke on always-active BTC 15m
    uv run python -m ml.lp.ws_logger --tickers KX...,KX... --minutes 5
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter

from dotenv import load_dotenv

from ingestion.kalshi import SERIES_BTC_15M, KalshiClient
from ingestion.kalshi_ws import KalshiWS, rest_top_of_book
from ml.lp.lp_gate import passes_gate
from ml.lp.lp_pilot import BENIGN_PREFIXES, EXCLUDE, JUMPY, is_mean_reverting

CHANNELS = ("orderbook_delta", "trade")


def discover_markets(
    client: KalshiClient, prefixes: tuple[str, ...] | None, cap: int
) -> list[str]:
    """Active, gate-eligible, mean-reverting (TOTAL/SPREAD) markets, most active first — the
    same universe the maker quotes, but the whole LIST instead of one pick."""
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    pfx = prefixes or BENIGN_PREFIXES
    counts: Counter[str] = Counter()
    for t in trades:
        tk = t.get("ticker", "")
        if not tk or any(x in tk for x in EXCLUDE) or any(j in tk for j in JUMPY):
            continue
        if any(tk.startswith(p) for p in pfx):
            counts[tk] += 1
    out: list[str] = []
    for tk, c in counts.most_common():
        if len(out) >= cap:
            break
        if passes_gate(tk, c) and is_mean_reverting(tk):
            out.append(tk)
    return out


async def run(
    *, tickers: list[str] | None, prefixes: tuple[str, ...] | None, use_btc: bool,
    minutes: float, cap: int, reconcile_s: float, refresh_s: float,
) -> int:
    client = KalshiClient(pace_seconds=0.1)
    fixed = tickers is not None or use_btc  # a pinned set doesn't re-discover/roll
    if tickers is not None:
        current = tickers
    elif use_btc:  # smoke path: BTC 15m is always active regardless of sports hours
        mkts = sorted(client.list_markets(SERIES_BTC_15M, status="open"),
                      key=lambda m: m.get("volume") or 0, reverse=True)
        current = [m["ticker"] for m in mkts[:cap]]
    else:
        current = discover_markets(client, prefixes, cap)
    if not current:
        print("no active eligible markets right now — idle. (try --prefix off-hours, or --btc)")
        client.close()
        return 0

    ws = KalshiWS(client)
    await ws.connect()
    await ws.subscribe(current, CHANNELS)
    pump = asyncio.create_task(ws.run())
    print(f"subscribed {len(current)} markets on 1 connection ({','.join(CHANNELS)}); "
          f"{minutes:.0f} min, reconcile every {reconcile_s:g}s\n")

    end = time.time() + minutes * 60
    last_refresh = time.time()
    try:
        while time.time() < end:
            await asyncio.sleep(reconcile_s)
            # reconcile a sample of books vs fresh REST snapshots (correctness at scale)
            sample = [tk for tk in current if tk in ws.books][:5]
            matches = 0
            for tk in sample:
                ws_tob = ws.books[tk].top_of_book()
                rest_tob = rest_top_of_book(client.get_market_orderbook(tk))
                matches += ws_tob == rest_tob
            trades = sum(ws.trade_counts.values())
            print(f"  [{time.strftime('%H:%M:%S')}] subscribed {len(current)} books {len(ws.books)}"
                  f" trades {trades} seq-gaps {ws._gaps} reconcile {matches}/{len(sample)}")
            # dynamic roll (discovery mode only): add new active markets, drop gone ones
            if not fixed and time.time() - last_refresh >= refresh_s:
                new = discover_markets(client, prefixes, cap)
                add = [t for t in new if t not in current]
                rem = [t for t in current if t not in new]
                if add:
                    await ws.update_subscription("orderbook_delta", add, "add_markets")
                    await ws.update_subscription("trade", add, "add_markets")
                if rem:
                    await ws.update_subscription("orderbook_delta", rem, "delete_markets")
                    await ws.update_subscription("trade", rem, "delete_markets")
                if add or rem:
                    print(f"    rolled: +{len(add)} -{len(rem)} markets")
                current = new
                last_refresh = time.time()
    finally:
        pump.cancel()
        await ws.close()
        client.close()
    print(f"\nDONE: {len(ws.books)} books built, {sum(ws.trade_counts.values())} trades, "
          f"{ws._gaps} seq gaps.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--prefix", default=None, help="restrict universe, e.g. KXWC")
    ap.add_argument("--tickers", default=None, help="explicit comma-separated tickers (pinned)")
    ap.add_argument("--btc", action="store_true", help="smoke on always-active BTC 15m markets")
    ap.add_argument("--cap", type=int, default=40)
    ap.add_argument("--reconcile", type=float, default=10.0)
    ap.add_argument("--refresh", type=float, default=90.0)
    args = ap.parse_args()
    load_dotenv()
    prefixes = tuple(p.strip() for p in args.prefix.split(",")) if args.prefix else None
    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    return asyncio.run(run(
        tickers=tickers, prefixes=prefixes, use_btc=args.btc, minutes=args.minutes,
        cap=args.cap, reconcile_s=args.reconcile, refresh_s=args.refresh,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
