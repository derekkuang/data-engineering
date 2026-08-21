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
    uv run python -m core.capture.ws_logger --minutes 30                 # active sports board
    uv run python -m core.capture.ws_logger --prefix KXWC --minutes 30   # World-Cup-only
    uv run python -m core.capture.ws_logger --btc --minutes 5   # smoke on always-active BTC 15m
    uv run python -m core.capture.ws_logger --tickers KX...,KX... --minutes 5
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from core.maker.lp_gate import MIN_RECENT_TRADES, passes_gate
from core.maker.lp_pilot import ELIGIBLE_PREFIXES, EXCLUDE, JUMPY, is_mean_reverting
from ingestion.kalshi import SERIES_BTC_15M, KalshiClient
from ingestion.kalshi_ws import KalshiWS, rest_top_of_book

CHANNELS = ("orderbook_delta", "trade")
BOOK_LOG = "data/ws_book.csv"
TRADE_LOG = "data/ws_trades.csv"
BOOK_HDR = ["ts_utc", "market", "bid", "ask", "mid", "spread_c"]
TRADE_HDR = ["ts_utc", "market", "trade_id", "price", "count", "taker_side", "trade_ts"]


def _append(path: str, header: list[str], rows: list[list[Any]]) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerows(rows)


def discover_markets(
    client: KalshiClient, prefixes: tuple[str, ...] | None, cap: int, *, wide: bool = False
) -> list[str]:
    """Active sports markets, most active first. Default = the maker's TRADING universe
    (gate-eligible + mean-reverting TOTAL/SPREAD only). ``wide`` = the MEASUREMENT universe:
    the activity floor only, KEEPING the known-toxic families (ITF, MATCH/GAME types) — they
    are the labeled positive controls that validate the markout instrument and give the
    toxicity model its toxic class. Never widen the maker; only widen the capture."""
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    pfx = prefixes or ELIGIBLE_PREFIXES
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
        if wide:
            if c >= MIN_RECENT_TRADES:
                out.append(tk)
        elif passes_gate(tk, c) and is_mean_reverting(tk):
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
    ws.set_subscription(current, CHANNELS)
    pump = asyncio.create_task(ws.run_forever())  # resilient: auto-reconnect + resubscribe
    print(f"subscribed {len(current)} markets on 1 connection ({','.join(CHANNELS)}); "
          f"{minutes:.0f} min -> {BOOK_LOG} + {TRADE_LOG}\n")

    end = time.time() + minutes * 60
    last_refresh = time.time()
    n_book = n_trade = 0
    try:
        while time.time() < end:
            await asyncio.sleep(reconcile_s)
            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            # log trades (complete, drained) + a top-of-book sample per market
            trade_rows = [
                [now_iso, t.get("market_ticker"), t.get("trade_id"), t.get("yes_price_dollars"),
                 t.get("count_fp"), t.get("taker_side"), t.get("ts_ms")]
                for t in ws.drain_trades()
            ]
            book_rows: list[list[Any]] = []
            for tk, bk in ws.books.items():
                bid, ask = bk.top_of_book()
                if bid is not None and ask is not None:
                    book_rows.append([now_iso, tk, f"{bid:.4f}", f"{ask:.4f}",
                                      f"{(bid + ask) / 2:.4f}", f"{(ask - bid) * 100:.2f}"])
            _append(TRADE_LOG, TRADE_HDR, trade_rows)
            _append(BOOK_LOG, BOOK_HDR, book_rows)
            n_trade += len(trade_rows)
            n_book += len(book_rows)
            # reconcile a sample of books vs fresh REST snapshots (correctness at scale)
            sample = [tk for tk in current if tk in ws.books][:5]
            matches = sum(
                ws.books[tk].top_of_book() == rest_top_of_book(client.get_market_orderbook(tk))
                for tk in sample
            )
            print(f"  [{time.strftime('%H:%M:%S')}] books {len(ws.books)} "
                  f"trades {sum(ws.trade_counts.values())} seq-gaps {ws._gaps} "
                  f"reconnects {ws._reconnects} reconcile {matches}/{len(sample)}")
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
        await ws.close()  # sets _stop -> run_forever exits its reconnect loop
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        client.close()
    print(f"\nDONE: {n_book} book ticks + {n_trade} trades logged, "
          f"{ws._gaps} seq gaps, {ws._reconnects} reconnects.")
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
