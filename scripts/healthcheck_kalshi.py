"""Kalshi public market-data healthcheck — connectivity + a real data read.

Kalshi's market-data endpoints (markets, candlesticks) are public on prod, so no
auth is needed; this pipeline is read-only (price/implied-prob, never orders).
Staged so a failure pinpoints the cause. Exit 0 = OK, 1 = FAIL.

Run:  uv run python scripts/healthcheck_kalshi.py   (reads KALSHI_API_BASE from .env)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingestion.kalshi import KalshiClient  # noqa: E402


def _ok(msg: str) -> None:
    print(f"  OK   {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def main() -> int:
    base = os.environ.get("KALSHI_API_BASE", "<unset>")
    print(f"Kalshi public healthcheck against {base}")

    # Stage 1 — config
    if not os.environ.get("KALSHI_API_BASE"):
        _fail("KALSHI_API_BASE not set in .env")
        return 1
    _ok("config present (public mode — no key required)")

    # Stage 2 — client init (public)
    try:
        client = KalshiClient()
    except Exception as exc:  # noqa: BLE001
        _fail(f"client init failed: {exc!r}")
        return 1
    _ok("client initialized (public, unauthenticated)")

    # Stage 3 — connectivity
    try:
        status = client.get_exchange_status()
    except Exception as exc:  # noqa: BLE001
        _fail(f"exchange status request failed: {exc!r}")
        return 1
    _ok(f"exchange reachable: {status}")

    # Stage 4 — real data read + look for Bitcoin markets
    try:
        resp = client.get_markets(limit=1000, status="open")
        markets = resp.get("markets", [])
    except Exception as exc:  # noqa: BLE001
        _fail(f"markets read failed: {exc!r}")
        return 1
    if not markets:
        _fail("markets read returned 0 rows")
        return 1
    btc = [m for m in markets if "BTC" in (m.get("ticker", "") + m.get("event_ticker", "")).upper()]
    sample = sorted({m.get("event_ticker", "") for m in btc})[:8]
    _ok(f"data read OK — {len(markets)} open markets; {len(btc)} look BTC-related")
    print(f"       sample BTC event tickers: {sample}")

    client.close()
    print("\nALL GREEN ✅  Kalshi public market data is reachable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
