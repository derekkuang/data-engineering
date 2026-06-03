"""Live order-book snapshot collector for KXBTC15M — the execution-timing probe.

The historical backtest priced every bet at the candlestick CLOSE — a tidy,
after-the-fact number. The one thing we can NOT reconstruct historically is the
LIVE, executable order book at the decision moment, so this captures exactly that:
for the currently-active KXBTC15M market it snapshots the top of book (plus total
resting depth) a few times a few seconds apart, alongside the BTC spot price, and
appends each snapshot as a JSONL row. The repeated snapshots measure how fast
Kalshi REPRICES — the lead-lag that decides whether the model's edge is actually
capturable or has already run away by the time you could trade.

Deliberately NOT captured here: the model prediction and the settlement outcome.
Both are reconstructable offline (BTC features from the warehouse; result from the
Kalshi markets endpoint), so we persist only the irreproducible live quote.

Read-only and public — no account, no money, no auth.

Usage:
    uv run python -m ingestion.kalshi_orderbook --snapshots 3 --interval 30
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from ingestion.kalshi import SERIES_BTC_15M, KalshiClient

COINBASE_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
OUT_PATH = Path("data/orderbook_snapshots.jsonl")


def _btc_spot(http: httpx.Client) -> float | None:
    """Current BTC-USD price from Coinbase's public ticker (best-effort)."""
    try:
        resp = http.get(COINBASE_TICKER, timeout=10.0)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def top_of_book(book: dict[str, Any]) -> dict[str, Any]:
    """Reduce a raw orderbook_fp dict to the touch + depth. A 'no' bid at price p is
    a 'yes' ask at (1 - p), so best_yes_ask = 1 - best_no_bid."""
    yes = [(float(p), float(q)) for p, q in (book.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (book.get("no_dollars") or [])]
    best_yes_bid = max((p for p, _ in yes), default=None)
    best_no_bid = max((p for p, _ in no), default=None)
    best_yes_ask = round(1.0 - best_no_bid, 4) if best_no_bid is not None else None

    mid: float | None = None
    spread: float | None = None
    if best_yes_bid is not None and best_yes_ask is not None:
        mid = round((best_yes_ask + best_yes_bid) / 2, 4)
        spread = round(best_yes_ask - best_yes_bid, 4)

    return {
        "best_yes_bid": best_yes_bid,
        "best_yes_ask": best_yes_ask,
        "yes_bid_size": dict(yes).get(best_yes_bid) if best_yes_bid is not None else None,
        "yes_ask_size": dict(no).get(best_no_bid) if best_no_bid is not None else None,
        "mid": mid,
        "spread": spread,
        "yes_levels": len(yes),
        "no_levels": len(no),
        "yes_depth": round(sum(q for _, q in yes), 1),
        "no_depth": round(sum(q for _, q in no), 1),
    }


def active_market(client: KalshiClient) -> dict[str, Any] | None:
    """The KXBTC15M market whose 15-min window currently contains `now` — i.e. the
    freshly-opened decision window. Filtering on open_time <= now < close_time (and
    taking the latest open_time) avoids grabbing a just-settled window that may still
    be briefly flagged 'open' at a 15-min boundary."""
    markets = client.list_markets(SERIES_BTC_15M, status="open")
    if not markets:
        return None

    def _t(iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    now = datetime.now(UTC)
    in_window = [m for m in markets if _t(m["open_time"]) <= now < _t(m["close_time"])]
    pool = in_window or markets  # fall back to all open if none strictly contain now
    return max(pool, key=lambda m: _t(m["open_time"]))


def collect(snapshots: int, interval: float, out_path: Path) -> int:
    load_dotenv()
    client = KalshiClient()
    http = httpx.Client()
    try:
        market = active_market(client)
        if market is None:
            print("No open KXBTC15M market right now (between windows) — try again shortly.")
            return 0

        ticker = market["ticker"]
        print(f"Active market: {ticker}")
        print(f"  window {market['open_time']} -> {market['close_time']}")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        records: list[tuple[datetime, dict[str, Any], float | None]] = []
        with out_path.open("a") as fh:
            for i in range(snapshots):
                now = datetime.now(UTC)
                tob = top_of_book(client.get_market_orderbook(ticker, depth=50))
                spot = _btc_spot(http)
                row = {
                    "captured_at": now.isoformat(),
                    "market_ticker": ticker,
                    "window_open_at": market["open_time"],
                    "window_close_at": market["close_time"],
                    "btc_spot": spot,
                    **tob,
                }
                fh.write(json.dumps(row) + "\n")
                fh.flush()
                records.append((now, tob, spot))

                if tob["spread"] is not None:
                    print(
                        f"  [{i + 1}/{snapshots}] {now:%H:%M:%S}Z  "
                        f"bid {tob['best_yes_bid']:.2f} ({tob['yes_bid_size']:.0f}) / "
                        f"ask {tob['best_yes_ask']:.2f} ({tob['yes_ask_size']:.0f})  "
                        f"spread {tob['spread'] * 100:.1f}c  mid {tob['mid']:.3f}  "
                        f"btc {spot}"
                    )
                else:
                    print(f"  [{i + 1}/{snapshots}] {now:%H:%M:%S}Z  no two-sided quote")

                if i < snapshots - 1:
                    time.sleep(interval)

        _print_drift(records)
        print(f"Wrote {len(records)} snapshot(s) to {out_path}")
        return 0
    finally:
        client.close()
        http.close()


def _print_drift(records: list[tuple[datetime, dict[str, Any], float | None]]) -> None:
    """Lead-lag readout: how far did the Kalshi mid (and BTC) move across snapshots?"""
    mids = [(t, r["mid"]) for t, r, _ in records if r["mid"] is not None]
    spots = [(t, s) for t, _, s in records if s is not None]
    if len(mids) < 2:
        return
    secs = (mids[-1][0] - mids[0][0]).total_seconds()
    mid_move = (mids[-1][1] - mids[0][1]) * 100
    line = f"Over {secs:.0f}s: Kalshi mid moved {mid_move:+.1f}c"
    if len(spots) >= 2:
        line += f"   BTC moved {spots[-1][1] - spots[0][1]:+.2f}"
    print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="KXBTC15M live order-book snapshot collector")
    parser.add_argument("--snapshots", type=int, default=3, help="number of book snapshots")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between snapshots")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help="JSONL output path")
    args = parser.parse_args()
    return collect(args.snapshots, args.interval, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
