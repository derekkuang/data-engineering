"""Coinbase Exchange API healthcheck.

Verifies network reachability, response schema, and data freshness for the
public candles endpoint. Exit code 0 on success, 1 on any failure — so this
script can be wired into CI or an Airflow sensor later.

Usage:
    uv run python scripts/healthcheck_coinbase.py
"""
import sys
from datetime import datetime, timedelta, timezone

import httpx

COINBASE_BASE = "https://api.exchange.coinbase.com"
PRODUCT = "BTC-USD"
GRANULARITY_SECONDS = 60
FRESHNESS_THRESHOLD = timedelta(minutes=5)
REQUEST_TIMEOUT_SECONDS = 10.0


def fetch_recent_candles(product: str, granularity: int) -> list[list[float]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=1)

    response = httpx.get(
        f"{COINBASE_BASE}/products/{product}/candles",
        params={
            "granularity": granularity,
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    print(f"Healthcheck: Coinbase Exchange API, {PRODUCT}, {GRANULARITY_SECONDS}s candles")

    try:
        candles = fetch_recent_candles(PRODUCT, GRANULARITY_SECONDS)
    except httpx.HTTPError as e:
        print(f"FAIL: HTTP error: {e}", file=sys.stderr)
        return 1

    if not candles:
        print("FAIL: response was empty", file=sys.stderr)
        return 1

    # Coinbase rows are [time, low, high, open, close, volume] — note LHOC, not OHLC.
    # https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles
    if len(candles[0]) != 6:
        print(
            f"FAIL: schema mismatch, expected 6 columns got {len(candles[0])}",
            file=sys.stderr,
        )
        return 1

    # Coinbase returns candles newest-first, so index 0 is the most recent.
    latest_ts = datetime.fromtimestamp(candles[0][0], tz=timezone.utc)
    latest_close = candles[0][4]
    age = datetime.now(timezone.utc) - latest_ts

    print(f"OK: received {len(candles)} candles")
    print(f"    latest candle ts: {latest_ts.isoformat()} (age {age})")
    print(f"    latest close:     ${latest_close:,.2f}")

    if age > FRESHNESS_THRESHOLD:
        print(
            f"FAIL: data is stale (age {age} > threshold {FRESHNESS_THRESHOLD})",
            file=sys.stderr,
        )
        return 1

    print("Healthcheck PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
