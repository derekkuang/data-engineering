"""Passive tennis market logger — captures a clean tick series for the OVERREACTION
research question: after a break-of-serve JUMP, does a tennis binary revert or continue?

This is the read-only opposite of ml/lp_live — it places NO orders and uses NO auth. It
polls the public order book + trade prints for every ACTIVE tennis market (ITF / ATP /
WTA / challenger — the trending, jumpy winner books that market-making can't make) and
appends two logs:
  * data/tennis_book.csv   one row per market per poll: bid/ask/mid/spread + top sizes
  * data/tennis_trades.csv one row per trade print:     price, size, taker side

From these we can later test the sign of the price autocorrelation around jumps —
reversion => a fade strategy is worth building; continuation => momentum; neither =>
tennis is closed for good. All measured BEFORE risking a cent. See the tennis post-mortem.

Usage:
    uv run python -m ml.research.tennis_logger --minutes 180             # log for 3h
    uv run python -m ml.research.tennis_logger --minutes 600 --poll 3    # overnight, faster cadence
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import KalshiClient

BOOK_LOG = "data/tennis_book.csv"
TRADE_LOG = "data/tennis_trades.csv"
TENNIS_TOKENS = ("ITF", "ATP", "WTA", "TENNIS")  # substrings that mark a tennis market
DEFAULT_POLL = 4.0  # seconds between book polls of each tracked market
REFRESH_S = 60.0  # re-scan the trade feed this often to (de)list active matches
DEFAULT_MAX = 40  # cap tracked markets (most active first) to bound API load

BOOK_HDR = ["ts_utc", "market", "bid", "ask", "mid", "spread_c", "bid_sz", "ask_sz"]
TRADE_HDR = ["ts_utc", "market", "trade_id", "created_time", "price", "count", "taker_side"]


def is_tennis(ticker: str) -> bool:
    return any(t in ticker for t in TENNIS_TOKENS)


def top_of_book(book: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """(bid, ask, bid_size, ask_size) in YES-dollars, or None if not two-sided. The YES
    bid is the best yes price; the YES ask is 1 - best no price (the no side's best bid)."""
    yes = book.get("yes_dollars") or book.get("yes") or []
    no = book.get("no_dollars") or book.get("no") or []
    if not yes or not no:
        return None
    by = max(yes, key=lambda x: float(x[0]))
    bn = max(no, key=lambda x: float(x[0]))
    return float(by[0]), round(1.0 - float(bn[0]), 4), float(by[1]), float(bn[1])


def active_tennis(client: KalshiClient, cap: int) -> list[str]:
    """Tennis markets with recent trade activity, most active first (capped). Resolved
    matches drop out automatically once they stop printing trades."""
    trades = client.get("/markets/trades", params={"limit": 1000}).get("trades", [])
    counts: dict[str, int] = defaultdict(int)
    for t in trades:
        tk = t.get("ticker", "")
        if is_tennis(tk):
            counts[tk] += 1
    return sorted(counts, key=lambda k: counts[k], reverse=True)[:cap]


def _trade_price(t: dict[str, Any]) -> float | None:
    if t.get("yes_price_dollars") is not None:
        return float(t["yes_price_dollars"])
    if t.get("yes_price") is not None:
        return float(t["yes_price"]) / 100.0
    return None


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


def _poll_market(
    client: KalshiClient, tk: str, now_iso: str, seen: set[str]
) -> tuple[list[Any] | None, list[list[Any]]]:
    """Return (book_row_or_None, new_trade_rows) for one market this cycle."""
    book_row: list[Any] | None = None
    tob = top_of_book(client.get_market_orderbook(tk))
    if tob:
        bid, ask, bsz, asz = tob
        book_row = [
            now_iso, tk, f"{bid:.4f}", f"{ask:.4f}", f"{(bid + ask) / 2:.4f}",
            f"{(ask - bid) * 100:.2f}", f"{bsz:.2f}", f"{asz:.2f}",
        ]
    trade_rows: list[list[Any]] = []
    raw = client.get("/markets/trades", params={"ticker": tk, "limit": 100})
    for tr in raw.get("trades", []):
        tid = tr.get("trade_id")
        px = _trade_price(tr)
        if not tid or tid in seen or px is None:
            continue
        seen.add(tid)
        trade_rows.append([
            now_iso, tk, tid, tr.get("created_time") or tr.get("ts"),
            f"{px:.4f}", tr.get("count_fp") or tr.get("count"), tr.get("taker_side"),
        ])
    return book_row, trade_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=180.0)
    ap.add_argument("--poll", type=float, default=DEFAULT_POLL)
    ap.add_argument("--max-markets", type=int, default=DEFAULT_MAX)
    args = ap.parse_args()

    load_dotenv()  # for KALSHI_API_BASE (public market data needs no auth)
    try:
        client = KalshiClient(pace_seconds=0.1)
    except Exception as exc:
        print(f"Failed to init client (need KALSHI_API_BASE in .env): {str(exc)[:120]}",
              file=sys.stderr)
        return 2

    end = time.time() + args.minutes * 60
    seen: dict[str, set[str]] = defaultdict(set)  # market -> logged trade_ids
    markets: list[str] = []
    last_refresh = 0.0
    n_book = n_trades = 0
    print(f"Tennis logger: {args.minutes:.0f}min, poll {args.poll:.0f}s, <={args.max_markets} mkts")
    print(f"READ-ONLY (no orders, no auth) -> {BOOK_LOG} + {TRADE_LOG}. Ctrl-C to stop.\n")
    try:
        while time.time() < end:
            t0 = time.time()
            if time.time() - last_refresh >= REFRESH_S:
                try:
                    markets = active_tennis(client, args.max_markets)
                    last_refresh = time.time()
                    print(f"  [{datetime.now(UTC):%H:%M:%S}] tracking {len(markets)} tennis mkts")
                except Exception as exc:
                    print(f"  refresh error: {str(exc)[:80]}")
            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            book_rows: list[list[Any]] = []
            trade_rows: list[list[Any]] = []
            for tk in markets:
                try:
                    br, trs = _poll_market(client, tk, now_iso, seen[tk])
                    if br:
                        book_rows.append(br)
                    trade_rows.extend(trs)
                except Exception as exc:
                    print(f"  {tk[:30]} poll error: {str(exc)[:60]}")
            _append(BOOK_LOG, BOOK_HDR, book_rows)
            _append(TRADE_LOG, TRADE_HDR, trade_rows)
            n_book += len(book_rows)
            n_trades += len(trade_rows)
            time.sleep(max(0.0, args.poll - (time.time() - t0)))
    except KeyboardInterrupt:
        print("\nstopped early.")
    finally:
        client.close()
    print(f"\nDONE: {n_book} book ticks + {n_trades} trades logged -> {BOOK_LOG}, {TRADE_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
