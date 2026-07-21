"""Real-time pre-quote MICROSTRUCTURE feature capture from the Kalshi WS feed.

The toxicity/selection model needs features knowable BEFORE we quote a market — and the
session log only has coarse post-hoc aggregates (which a walk-forward study showed do NOT
predict per-market markout). This module captures the finer signals that might: from the
live order book (via ingestion.kalshi_ws) it engineers, per market per snapshot,

  * book shape   — spread, mid, size IMBALANCE (yes-depth vs no-depth), near-touch depth,
                   level count;
  * trade flow   — trades/min, volume, taker-buy fraction, signed net flow over a window;
  * mid dynamics — realized mid-volatility and drift over the window.

Rows land in data/ws_features.csv, keyed by (ts_utc, market) so they JOIN to our realized
markout (fct_lp_market_session / lp_fills) point-in-time — the feature store for the model.
This is ALSO the WS-data layer of the platform: run it during games to accumulate; a dbt
staging model over this CSV feeds both the dashboard and the model.

It places NO orders and needs the same read-only WS as ws_logger. The MODEL on top is a
separate, data-gated experiment; this pipeline is a standalone DE artifact regardless.

Usage:
    uv run python -m ml.lp.ws_features --prefix KXWC --minutes 60   # capture during WC games
    uv run python -m ml.lp.ws_features --btc --minutes 5            # smoke on always-active BTC
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from statistics import pstdev
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import SERIES_BTC_15M, KalshiClient
from ingestion.kalshi_ws import KalshiWS, LocalBook
from ml.lp.ws_logger import CHANNELS, discover_markets

WINDOW_S = 60.0  # trailing window for flow + volatility features
FEATURE_LOG = "data/ws_features.csv"
NEAR_TOUCH = 0.03  # dollars; "near touch" = within 3c of the best price on a side
FEATURE_COLS = [
    "ts_utc", "market", "bid", "ask", "mid", "spread_c",
    "imbalance", "yes_depth", "no_depth", "depth_near", "n_levels",
    "trades_1m", "vol_1m", "taker_buy_frac", "signed_flow_1m",
    "midvol_1m", "midmove_1m",
]


def book_features(book: LocalBook) -> dict[str, float] | None:
    """Point-in-time order-book features, or None if the book is one-sided/empty (no usable
    two-sided signal — the same books the maker skips). `imbalance` in [-1,1]: >0 = more
    depth wanting to BUY yes (upward book pressure); depth in contracts."""
    yes = [(float(p), float(s)) for p, s in book.yes.items() if float(s) > 0]
    no = [(float(p), float(s)) for p, s in book.no.items() if float(s) > 0]
    if not yes or not no:
        return None
    best_yes = max(p for p, _ in yes)
    best_no = max(p for p, _ in no)
    bid = best_yes
    ask = round(1.0 - best_no, 4)  # YES ask = 1 - best NO bid (same convention as the maker)
    yes_depth = sum(s for _, s in yes)
    no_depth = sum(s for _, s in no)
    tot = yes_depth + no_depth
    near = (sum(s for p, s in yes if p >= best_yes - NEAR_TOUCH)
            + sum(s for p, s in no if p >= best_no - NEAR_TOUCH))
    return {
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2.0,
        "spread_c": (ask - bid) * 100.0,
        "imbalance": (yes_depth - no_depth) / tot if tot else 0.0,
        "yes_depth": yes_depth,
        "no_depth": no_depth,
        "depth_near": near,
        "n_levels": float(len(yes) + len(no)),
    }


class MarketFeatureState:
    """Rolling per-market window of recent trades + mids, for flow and volatility features.
    Evicts anything older than `window_s` on every update so features are always trailing-window."""

    def __init__(self, window_s: float = WINDOW_S) -> None:
        self.window_s = window_s
        self.trades: deque[tuple[float, float, str]] = deque()  # (ts, count, taker_side)
        self.mids: deque[tuple[float, float]] = deque()  # (ts, mid)

    def _evict(self, now: float) -> None:
        cut = now - self.window_s
        while self.trades and self.trades[0][0] < cut:
            self.trades.popleft()
        while self.mids and self.mids[0][0] < cut:
            self.mids.popleft()

    def add_trades(self, trades: list[tuple[float, float, str]], now: float) -> None:
        self.trades.extend(trades)
        self._evict(now)

    def add_mid(self, mid: float, now: float) -> None:
        self.mids.append((now, mid))
        self._evict(now)

    def flow_features(self) -> dict[str, float]:
        """Taker-flow over the window. taker_buy_frac>0.5 = aggressive yes-buying; signed_flow
        = net taker direction in contracts (the adverse-selection pressure a maker faces)."""
        vol = sum(c for _, c, _ in self.trades)
        buy = sum(c for _, c, s in self.trades if s == "yes")
        signed = sum((c if s == "yes" else -c) for _, c, s in self.trades)
        return {
            "trades_1m": float(len(self.trades)),
            "vol_1m": vol,
            "taker_buy_frac": (buy / vol) if vol else 0.5,
            "signed_flow_1m": signed,
        }

    def vol_features(self) -> dict[str, float]:
        """Realized mid-volatility (cents) and drift over the window — jumpy/trending books
        (the pick-off regime) show up here before it costs us a fill."""
        mids = [m for _, m in self.mids]
        midvol = pstdev(mids) if len(mids) >= 2 else 0.0
        midmove = (mids[-1] - mids[0]) if len(mids) >= 2 else 0.0
        return {"midvol_1m": midvol * 100.0, "midmove_1m": midmove * 100.0}


def _bucket_trades(raw: list[dict[str, Any]]) -> dict[str, list[tuple[float, float, str]]]:
    """Group drained WS trade msgs by market into (ts_seconds, count, taker_side) tuples."""
    out: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for t in raw:
        tk = t.get("market_ticker")
        if not tk:
            continue
        ts_ms = t.get("ts_ms")
        ts = float(ts_ms) / 1000.0 if ts_ms is not None else time.time()
        count = float(t.get("count_fp") or t.get("count") or 0)
        side = str(t.get("taker_side") or "")
        out[tk].append((ts, count, side))
    return out


def _append(path: str, rows: list[list[Any]]) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FEATURE_COLS)
        w.writerows(rows)


async def run(
    *, tickers: list[str] | None, prefixes: tuple[str, ...] | None, use_btc: bool,
    minutes: float, cap: int, snapshot_s: float, refresh_s: float, wide: bool = False,
) -> int:
    client = KalshiClient(pace_seconds=0.1)
    fixed = tickers is not None or use_btc
    if tickers is not None:
        current = tickers
    elif use_btc:
        mkts = sorted(client.list_markets(SERIES_BTC_15M, status="open"),
                      key=lambda m: m.get("volume") or 0, reverse=True)
        current = [m["ticker"] for m in mkts[:cap]]
    else:
        current = discover_markets(client, prefixes, cap, wide=wide)
    if not current:
        print("no active eligible markets right now — idle. (try --prefix off-hours, or --btc)")
        client.close()
        return 0

    ws = KalshiWS(client)
    ws.set_subscription(current, CHANNELS)
    pump = asyncio.create_task(ws.run_forever())
    states: dict[str, MarketFeatureState] = {}
    print(f"capturing features on {len(current)} markets -> {FEATURE_LOG} "
          f"(every {snapshot_s:g}s, {WINDOW_S:g}s window); {minutes:.0f} min\n")

    end = time.time() + minutes * 60
    last_refresh = time.time()
    n_rows = 0
    try:
        while time.time() < end:
            await asyncio.sleep(snapshot_s)
            now = time.time()
            now_iso = datetime.now(UTC).isoformat(timespec="seconds")
            by_mkt = _bucket_trades(ws.drain_trades())
            rows: list[list[Any]] = []
            for tk, book in list(ws.books.items()):
                bf = book_features(book)
                if bf is None:
                    continue
                st = states.setdefault(tk, MarketFeatureState())
                st.add_trades(by_mkt.get(tk, []), now)
                st.add_mid(bf["mid"], now)
                feats = {**bf, **st.flow_features(), **st.vol_features()}
                rows.append([now_iso, tk] + [round(feats[c], 4) for c in FEATURE_COLS[2:]])
            _append(FEATURE_LOG, rows)
            n_rows += len(rows)
            print(f"  [{time.strftime('%H:%M:%S')}] {len(rows)} feature rows "
                  f"({len(ws.books)} books, {sum(ws.trade_counts.values())} trades seen, "
                  f"seq-gaps {ws._gaps})")
            if not fixed and now - last_refresh >= refresh_s:
                new = discover_markets(client, prefixes, cap, wide=wide)
                add = [t for t in new if t not in current]
                rem = [t for t in current if t not in new]
                if add:
                    await ws.update_subscription("orderbook_delta", add, "add_markets")
                    await ws.update_subscription("trade", add, "add_markets")
                if rem:
                    await ws.update_subscription("orderbook_delta", rem, "delete_markets")
                    await ws.update_subscription("trade", rem, "delete_markets")
                current, last_refresh = new, now
    finally:
        await ws.close()
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
        client.close()
    print(f"\nDONE: {n_rows} feature rows -> {FEATURE_LOG}. Join to fct_lp_market_session on "
          "(market, time) to train the toxicity model once enough games are captured.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--prefix", default=None, help="restrict universe, e.g. KXWC")
    ap.add_argument("--tickers", default=None, help="explicit comma-separated tickers (pinned)")
    ap.add_argument("--btc", action="store_true", help="smoke on always-active BTC 15m markets")
    ap.add_argument("--cap", type=int, default=40)
    ap.add_argument("--snapshot", type=float, default=5.0, help="seconds between feature rows")
    ap.add_argument("--refresh", type=float, default=90.0, help="seconds between re-discovery")
    ap.add_argument("--wide", action="store_true",
                    help="MEASUREMENT universe: activity floor only, keeping known-toxic "
                         "families (ITF, MATCH/GAME) as labeled markout controls")
    args = ap.parse_args()
    load_dotenv()
    prefixes = tuple(p.strip() for p in args.prefix.split(",")) if args.prefix else None
    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    return asyncio.run(run(
        tickers=tickers, prefixes=prefixes, use_btc=args.btc, minutes=args.minutes,
        cap=args.cap, snapshot_s=args.snapshot, refresh_s=args.refresh, wide=args.wide,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
