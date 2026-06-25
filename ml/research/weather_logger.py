"""Passive Kalshi weather (daily high-temperature) market logger — captures the tick
series needed to decide whether these books are MAKEABLE before risking a cent.

Weather temp markets are the one NON-sports candidate for our mean-reversion maker: a
daily high-temp ladder per city (KXHIGHNY, KXHIGHCHI, ...) of mutually-exclusive 2°F
BUCKETS ("81° to 82°") + tail thresholds ("87° or above"), each a YES/NO that settles on
the station's high at end of day (~1am ET next day). The maker thesis: the near-money
bucket's mid sits stable around the day's best estimate (a physical anchor → mean-revert →
harvest spread), and Kalshi may pay a maker rebate on top. The KILLER risk: end-of-day
CONVERGENCE — as the actual high becomes known the near-money bucket snaps to 0/1 and picks
off resting quotes — plus forecast-driven (informed) flow. Whether spread (+ any rebate)
beats that adverse selection is the open question; this logger measures it.

Read-only: NO orders, NO auth (public market data). The Kalshi analog of tennis_logger /
poly_reward_logger. For every weather bucket with a live two-sided book it appends:
  * data/weather_book.csv   one row per market per poll: bid/ask/mid/spread + top sizes
  * data/weather_trades.csv one row per trade print: price, count, taker side, time

Then ml/weather_analyze.py (later) computes markout (esp. into the close = convergence
pick-off), spread distribution, and flow — the decisive read on whether weather MM works
or is the same thin-spread-subsidy-vs-toxicity trap as Polymarket reward farming.

Usage:
    uv run python -m ml.research.weather_logger --minutes 600        # log a full day
    uv run python -m ml.research.weather_logger --minutes 180 --poll 4
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import KalshiClient

BOOK_LOG = "data/weather_book.csv"
TRADE_LOG = "data/weather_trades.csv"
# Daily high-temperature series (one ladder of temp buckets per city per day).
WEATHER_SERIES = (
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHLAX", "KXHIGHDEN", "KXHIGHPHIL",
)
DEFAULT_POLL = 4.0  # seconds between book polls of each tracked market
REFRESH_S = 60.0  # re-scan the weather ladders this often (buckets go live as the day firms)
DEFAULT_MAX = 40  # cap tracked markets (bound API load)

BOOK_HDR = [
    "ts_utc", "market", "city", "bucket", "bid", "ask", "mid", "spread_c", "bid_sz", "ask_sz",
]
TRADE_HDR = [
    "ts_utc", "market", "city", "trade_id", "created_time", "price", "count", "taker_side",
]


def top_of_book(book: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """(bid, ask, bid_size, ask_size) in YES-dollars, or None if not two-sided. YES ask =
    1 - best NO bid (the same convention as the maker bot's best_bid_ask)."""
    yes = book.get("yes_dollars") or book.get("yes") or []
    no = book.get("no_dollars") or book.get("no") or []
    if not yes or not no:
        return None
    by = max(yes, key=lambda x: float(x[0]))
    bn = max(no, key=lambda x: float(x[0]))
    return float(by[0]), round(1.0 - float(bn[0]), 4), float(by[1]), float(bn[1])


def active_weather(client: KalshiClient, cap: int) -> list[tuple[str, str, str]]:
    """(ticker, city, bucket) for weather buckets with a LIVE two-sided quote, most volume
    first (capped). Uses the cheap list_markets summary (yes_bid/yes_ask present) to pick the
    active subset — overnight, when the ladders are empty, this returns [] and we idle."""
    found: list[tuple[float, str, str, str]] = []  # (volume, ticker, city, bucket)
    for series in WEATHER_SERIES:
        try:
            markets = client.list_markets(series, status="open")
        except Exception:
            continue
        for m in markets:
            if m.get("yes_bid") is None or m.get("yes_ask") is None:
                continue  # no live two-sided quote -> not active
            tk = m.get("ticker", "")
            bucket = str(m.get("subtitle") or m.get("yes_sub_title") or "")
            found.append((float(m.get("volume") or 0), tk, series, bucket))
    found.sort(reverse=True, key=lambda x: x[0])
    return [(tk, city, bucket) for _, tk, city, bucket in found[:cap]]


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
    client: KalshiClient, tk: str, city: str, bucket: str, now_iso: str, seen: set[str]
) -> tuple[list[Any] | None, list[list[Any]]]:
    """Return (book_row_or_None, new_trade_rows) for one weather bucket this cycle."""
    book_row: list[Any] | None = None
    tob = top_of_book(client.get_market_orderbook(tk))
    if tob:
        bid, ask, bsz, asz = tob
        book_row = [
            now_iso, tk, city, bucket, f"{bid:.4f}", f"{ask:.4f}", f"{(bid + ask) / 2:.4f}",
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
            now_iso, tk, city, tid, tr.get("created_time") or tr.get("ts"),
            f"{px:.4f}", tr.get("count_fp") or tr.get("count"), tr.get("taker_side"),
        ])
    return book_row, trade_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=600.0)
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
    seen: dict[str, set[str]] = {}
    markets: list[tuple[str, str, str]] = []
    last_refresh = 0.0
    n_book = n_trades = 0
    print(f"Weather logger: {args.minutes:.0f}min, poll {args.poll:.0f}s, "
          f"<={args.max_markets} mkts")
    print(f"READ-ONLY (no orders, no auth) -> {BOOK_LOG} + {TRADE_LOG}. Ctrl-C to stop.\n")
    try:
        while time.time() < end:
            t0 = time.time()
            if time.time() - last_refresh >= REFRESH_S:
                try:
                    markets = active_weather(client, args.max_markets)
                    last_refresh = time.time()
                    print(f"  [{datetime.now(UTC):%H:%M:%S}] tracking "
                          f"{len(markets)} live weather buckets")
                except Exception as exc:
                    print(f"  refresh error: {str(exc)[:80]}")
            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            book_rows: list[list[Any]] = []
            trade_rows: list[list[Any]] = []
            for tk, city, bucket in markets:
                try:
                    seen_tk = seen.setdefault(tk, set())
                    br, trs = _poll_market(client, tk, city, bucket, now_iso, seen_tk)
                    if br:
                        book_rows.append(br)
                    trade_rows.extend(trs)
                except Exception as exc:
                    print(f"  {tk[:28]} poll error: {str(exc)[:50]}")
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
