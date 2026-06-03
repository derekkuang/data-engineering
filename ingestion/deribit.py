"""Deribit BTC-PERPETUAL funding-rate history client (public, read-only).

A US-accessible derivatives source — Binance and Bybit futures APIs geo-block US
IPs (HTTP 451/403), while Deribit's public JSON-RPC endpoint responds. Funding is
the cleanest historically-backfillable derivatives signal: it encodes perp
positioning pressure (longs paying shorts when positive). get_funding_rate_history
returns HOURLY points with the 8-hour funding rate (interest_8h) and the BTC index
price. Each request is capped at ~744 points (~31 days), so backfill chunks by time.

This caches to a local Parquet for the quick "does funding add signal?" test; if it
earns its keep, it graduates to the S3 → Glue → dbt path like the other sources.

Usage:
    uv run python -m ingestion.deribit --start 2026-03-25 --end 2026-05-31
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

DERIBIT_URL = "https://www.deribit.com/api/v2/public/get_funding_rate_history"
INSTRUMENT = "BTC-PERPETUAL"
CHUNK_DAYS = 25  # 25 * 24 = 600 hourly points, under the ~744 response cap
OUT_PATH = Path("data/deribit_funding.parquet")
COLUMNS = ["funding_time", "interest_8h", "interest_1h", "index_price"]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fetch_funding_history(start: datetime, end: datetime, timeout: float = 15.0) -> pd.DataFrame:
    """Hourly funding history in [start, end], chunked to respect the response cap.
    Columns: funding_time (UTC), interest_8h, interest_1h, index_price."""
    http = httpx.Client(timeout=timeout)
    frames: list[pd.DataFrame] = []
    try:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
            resp = http.get(
                DERIBIT_URL,
                params={
                    "instrument_name": INSTRUMENT,
                    "start_timestamp": _ms(cursor),
                    "end_timestamp": _ms(chunk_end),
                },
            )
            resp.raise_for_status()
            result = resp.json().get("result", [])
            if result:
                frames.append(pd.DataFrame(result))
            cursor = chunk_end
    finally:
        http.close()

    if not frames:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    df["funding_time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df[COLUMNS].drop_duplicates("funding_time").sort_values("funding_time")
    return df.reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Deribit BTC-PERPETUAL funding history")
    parser.add_argument("--start", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end", required=True, help="UTC date YYYY-MM-DD (exclusive)")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    df = fetch_funding_history(start, end)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out)

    print(f"Wrote {len(df)} hourly funding points to {args.out}")
    if len(df):
        print(f"  range: {df['funding_time'].min()} .. {df['funding_time'].max()}")
        print(
            f"  interest_8h: min {df['interest_8h'].min():.2e}  "
            f"max {df['interest_8h'].max():.2e}  mean {df['interest_8h'].mean():.2e}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
