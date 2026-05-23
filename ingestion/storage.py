"""Write normalized OHLCV bars to the S3 raw zone as date-partitioned Parquet.

Layout::

    s3://<bucket>/raw/coinbase_ohlcv/dt=YYYY-MM-DD/<asset>.parquet

One file per ``(asset, day)``, overwritten on re-run — so backfills are
idempotent. The ``dt`` partition lives in the S3 key (Hive-style), *not* in the
Parquet columns; Athena derives it via partition projection (see
``docs/setup/03-athena-s3.md``).

``PARQUET_SCHEMA`` is the explicit contract with the Athena external-table DDL —
if a column or type changes here, change it there too.
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

from ingestion.coinbase import OhlcvBar

# boto3 clients are dynamically generated and ship no real type — typing as Any
# is the idiomatic path without pulling in the heavy boto3-stubs package.
S3Client = Any

logger = logging.getLogger(__name__)

RAW_PREFIX = "raw/coinbase_ohlcv"

# Explicit schema = the contract with the Athena CREATE EXTERNAL TABLE DDL.
PARQUET_SCHEMA = pa.schema(
    [
        ("asset_id", pa.string()),
        ("event_at", pa.timestamp("us", tz="UTC")),  # microsecond precision (discipline #4)
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)


def _require_bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set — copy .env.example to .env and fill it in")
    return bucket


def _bars_to_table(bars: list[OhlcvBar], ingested_at: datetime) -> pa.Table:
    frame = pd.DataFrame(
        {
            "asset_id": [b.asset_id for b in bars],
            "event_at": [b.event_at for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
            "ingested_at": [ingested_at] * len(bars),
        }
    )
    return pa.Table.from_pandas(frame, schema=PARQUET_SCHEMA, preserve_index=False)


def write_bars_to_s3(
    bars: list[OhlcvBar],
    *,
    s3_client: S3Client | None = None,
    ingested_at: datetime | None = None,
) -> int:
    """Group bars by ``(asset, day)`` and write one Parquet object per group to S3.

    Returns the number of partition files written.
    """
    if not bars:
        logger.info("no bars to write")
        return 0

    s3 = s3_client or boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    bucket = _require_bucket()
    ingested_at = ingested_at or datetime.now(UTC)

    groups: dict[tuple[str, date], list[OhlcvBar]] = defaultdict(list)
    for bar in bars:
        groups[(bar.asset_id, bar.event_at.date())].append(bar)

    files_written = 0
    for (asset_id, day), day_bars in sorted(groups.items()):
        table = _bars_to_table(day_bars, ingested_at)
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]

        key = f"{RAW_PREFIX}/dt={day.isoformat()}/{asset_id}.parquet"
        s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
        files_written += 1
        logger.info("wrote s3://%s/%s (%d rows)", bucket, key, len(day_bars))

    logger.info("wrote %d partition file(s) to s3://%s/%s", files_written, bucket, RAW_PREFIX)
    return files_written
