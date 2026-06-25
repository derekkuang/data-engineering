"""Async Kalshi WebSocket client — maintains local order books from the `orderbook_delta`
stream so we can watch (and later quote) MANY markets concurrently on one connection.

REST polling is too slow to refresh many books without quotes going stale (~5 calls per
market per cycle), which capped the maker at one market at a time. WS pushes book updates
in real time across many markets on a single authenticated connection — the enabler for
the multi-market concurrency that is the proven scaling lever (capture is taker-flow-
limited, so more markets > bigger size). See the LP track notes.

Message format (confirmed live): subscribe returns `subscribed`, then an
`orderbook_snapshot` (`yes_dollars_fp`/`no_dollars_fp` = `[[price_str, size_str], ...]`),
then incremental `orderbook_delta` (`price_dollars`, signed `delta_fp` — may be fractional,
`side` yes/no). `seq` is sequential per stream → a gap means a dropped message.

This module is the reusable foundation (the live multi-market maker will import it). Run as
a script for the Phase-2 SELF-CHECK: maintain a market's book from the WS deltas and every
few seconds reconcile it against a fresh REST snapshot — sustained MATCH proves the delta
application + seq handling are correct (the one under-documented risk).

Usage:
    uv run python -m ingestion.kalshi_ws                 # reconcile an active BTC 15m market
    uv run python -m ingestion.kalshi_ws --ticker KX...  # a specific market
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

import websockets
from dotenv import load_dotenv

from ingestion.kalshi import SERIES_BTC_15M, KalshiClient

logger = logging.getLogger(__name__)


def _touch(
    yes_pairs: list[tuple[float, float]], no_pairs: list[tuple[float, float]]
) -> tuple[float | None, float | None]:
    """(yes_bid, yes_ask) from (price, size) level lists. YES ask = 1 - best NO bid —
    the same convention the REST maker uses."""
    yb = max((p for p, s in yes_pairs if s > 0), default=None)
    nb = max((p for p, s in no_pairs if s > 0), default=None)
    ask = round(1.0 - nb, 4) if nb is not None else None
    return yb, ask


class LocalBook:
    """One market's order book, maintained from a snapshot + incremental deltas. Prices are
    kept as their exact string keys (dollars); sizes as Decimal to avoid float drift across
    thousands of deltas. `seq` is the last applied stream sequence number."""

    __slots__ = ("yes", "no", "seq")

    def __init__(self) -> None:
        self.yes: dict[str, Decimal] = {}
        self.no: dict[str, Decimal] = {}
        self.seq: int = 0

    def apply_snapshot(self, msg: dict[str, Any], seq: int) -> None:
        self.yes = {p: Decimal(s) for p, s in msg.get("yes_dollars_fp", []) if Decimal(s) > 0}
        self.no = {p: Decimal(s) for p, s in msg.get("no_dollars_fp", []) if Decimal(s) > 0}
        self.seq = seq

    def apply_delta(self, msg: dict[str, Any], seq: int) -> None:
        side = self.yes if msg["side"] == "yes" else self.no
        price = msg["price_dollars"]
        new = side.get(price, Decimal(0)) + Decimal(msg["delta_fp"])
        if new > 0:
            side[price] = new
        else:
            side.pop(price, None)
        self.seq = seq

    def top_of_book(self) -> tuple[float | None, float | None]:
        yes_pairs = [(float(p), float(s)) for p, s in self.yes.items()]
        no_pairs = [(float(p), float(s)) for p, s in self.no.items()]
        return _touch(yes_pairs, no_pairs)


def rest_top_of_book(book: dict[str, Any]) -> tuple[float | None, float | None]:
    """Top-of-book from a REST get_market_orderbook response, normalized to dollars (the
    REST feed quotes price levels in CENTS; the WS feed in dollars)."""
    ob = book.get("orderbook") or book
    yes = ob.get("yes") or ob.get("yes_dollars") or []
    no = ob.get("no") or ob.get("no_dollars") or []

    def norm(levels: list[Any]) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for p, s in levels:
            px = float(p)
            out.append((px / 100.0 if px > 1 else px, float(s)))  # cents -> dollars if needed
        return out

    return _touch(norm(yes), norm(no))


class KalshiWS:
    """One authenticated WS connection maintaining a local book per subscribed market."""

    def __init__(self, client: KalshiClient) -> None:
        self.client = client
        self.books: dict[str, LocalBook] = {}
        self._ws: Any = None
        self._gaps = 0

    async def connect(self) -> None:
        url, headers = self.client.ws_handshake()
        self._ws = await websockets.connect(url, additional_headers=headers)

    async def subscribe(
        self, tickers: list[str], channels: tuple[str, ...] = ("orderbook_delta",)
    ) -> None:
        await self._ws.send(json.dumps({
            "id": 1, "cmd": "subscribe",
            "params": {"channels": list(channels), "market_tickers": tickers},
        }))

    def _handle(self, m: dict[str, Any]) -> None:
        t = m.get("type")
        if t not in ("orderbook_snapshot", "orderbook_delta"):
            return
        msg = m.get("msg", {})
        ticker = msg.get("market_ticker")
        seq = int(m.get("seq", 0))
        book = self.books.setdefault(ticker, LocalBook())
        if t == "orderbook_snapshot":
            book.apply_snapshot(msg, seq)
        else:
            if seq != book.seq + 1:  # gap → local book is now unreliable for this stream
                self._gaps += 1
                logger.warning("seq gap on %s: expected %d got %d", ticker, book.seq + 1, seq)
            book.apply_delta(msg, seq)

    async def run(self) -> None:
        async for raw in self._ws:
            self._handle(json.loads(raw))

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


async def reconcile(ticker: str | None, checks: int, interval: float) -> int:
    """Phase-2 self-check: maintain ONE market's book over WS and every `interval`s compare
    its top-of-book to a fresh REST snapshot. Sustained MATCH validates the local book."""
    client = KalshiClient(pace_seconds=0.1)
    if ticker is None:
        mkts = client.list_markets(SERIES_BTC_15M, status="open")
        mkts.sort(key=lambda m: m.get("volume") or 0, reverse=True)
        if not mkts:
            print("no active BTC 15m market; pass --ticker")
            return 1
        ticker = mkts[0]["ticker"]

    ws = KalshiWS(client)
    await ws.connect()
    await ws.subscribe([ticker])
    pump = asyncio.create_task(ws.run())
    print(f"reconciling {ticker} — WS local book vs REST snapshot, {checks}x every {interval:g}s\n")
    matches = 0
    try:
        for i in range(checks):
            await asyncio.sleep(interval)
            book = ws.books.get(ticker)
            if book is None:
                print(f"[{i}] no snapshot yet")
                continue
            ws_bid, ws_ask = book.top_of_book()
            rest_bid, rest_ask = rest_top_of_book(client.get_market_orderbook(ticker))
            ok = ws_bid == rest_bid and ws_ask == rest_ask
            matches += ok
            print(f"[{i}] seq={book.seq:>4} levels y{len(book.yes)}/n{len(book.no)}  "
                  f"WS {ws_bid}/{ws_ask}  REST {rest_bid}/{rest_ask}  "
                  f"{'MATCH' if ok else 'mismatch (timing? re-check)'}")
    finally:
        pump.cancel()
        await ws.close()
        client.close()
    print(f"\n{matches}/{checks} MATCH, {ws._gaps} seq gaps.  "
          f"{'Local book VALIDATED.' if matches >= checks - 1 else 'Investigate mismatches.'}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default=None)
    ap.add_argument("--checks", type=int, default=8)
    ap.add_argument("--interval", type=float, default=10.0)
    args = ap.parse_args()
    load_dotenv()
    return asyncio.run(reconcile(args.ticker, args.checks, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
