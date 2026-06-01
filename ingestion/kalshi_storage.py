"""Write normalized Kalshi 15-min BTC candles to the S3 raw zone as Parquet.

Layout::

    s3://<bucket>/raw/kalshi_btc_15min/dt=YYYY-MM-DD/candles.parquet

One file per ``dt`` (the 15-min window's open date), overwritten on re-run, so
backfills are idempotent. ``dt`` lives in the S3 key (Hive-style), not the Parquet
columns; Athena derives it via partition projection. ``PARQUET_SCHEMA`` is the
explicit contract with the Glue external-table DDL.
"""

from __future__ import annotations

import io
import logging
import os
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ingestion.kalshi import KalshiCandle

S3Client = Any

logger = logging.getLogger(__name__)

RAW_PREFIX = "raw/kalshi_btc_15min"

PARQUET_SCHEMA = pa.schema(
    [
        ("market_ticker", pa.string()),
        ("series_ticker", pa.string()),
        ("window_open_at", pa.timestamp("us", tz="UTC")),
        ("window_close_at", pa.timestamp("us", tz="UTC")),
        ("event_at", pa.timestamp("us", tz="UTC")),
        ("implied_prob_open", pa.float64()),
        ("implied_prob_high", pa.float64()),
        ("implied_prob_low", pa.float64()),
        ("implied_prob_close", pa.float64()),
        ("implied_prob_mean", pa.float64()),
        ("yes_bid_close", pa.float64()),
        ("yes_ask_close", pa.float64()),
        ("volume", pa.float64()),
        ("open_interest", pa.float64()),
        ("result", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)


def _require_bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set — copy .env.example to .env and fill it in")
    return bucket


def _candles_to_table(candles: list[KalshiCandle], ingested_at: datetime) -> pa.Table:
    frame = pd.DataFrame(
        {
            "market_ticker": [c.market_ticker for c in candles],
            "series_ticker": [c.series_ticker for c in candles],
            "window_open_at": [c.window_open_at for c in candles],
            "window_close_at": [c.window_close_at for c in candles],
            "event_at": [c.event_at for c in candles],
            "implied_prob_open": [c.implied_prob_open for c in candles],
            "implied_prob_high": [c.implied_prob_high for c in candles],
            "implied_prob_low": [c.implied_prob_low for c in candles],
            "implied_prob_close": [c.implied_prob_close for c in candles],
            "implied_prob_mean": [c.implied_prob_mean for c in candles],
            "yes_bid_close": [c.yes_bid_close for c in candles],
            "yes_ask_close": [c.yes_ask_close for c in candles],
            "volume": [c.volume for c in candles],
            "open_interest": [c.open_interest for c in candles],
            "result": [c.result for c in candles],
            "ingested_at": [ingested_at] * len(candles),
        }
    )
    return pa.Table.from_pandas(frame, schema=PARQUET_SCHEMA, preserve_index=False)


def write_candles_to_s3(
    candles: list[KalshiCandle],
    *,
    s3_client: S3Client | None = None,
    ingested_at: datetime | None = None,
) -> int:
    """Group candles by their window's open date and write one Parquet per dt.

    Returns the number of partition files written.
    """
    if not candles:
        logger.info("no candles to write")
        return 0

    s3 = s3_client or boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    bucket = _require_bucket()
    ingested_at = ingested_at or datetime.now(UTC)

    groups: dict[date, list[KalshiCandle]] = defaultdict(list)
    for candle in candles:
        groups[candle.window_open_at.date()].append(candle)

    files_written = 0
    for day, day_candles in sorted(groups.items()):
        table = _candles_to_table(day_candles, ingested_at)
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
        key = f"{RAW_PREFIX}/dt={day.isoformat()}/candles.parquet"
        s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
        files_written += 1
        logger.info("wrote s3://%s/%s (%d rows)", bucket, key, len(day_candles))

    logger.info("wrote %d partition file(s) to s3://%s/%s", files_written, bucket, RAW_PREFIX)
    return files_written
