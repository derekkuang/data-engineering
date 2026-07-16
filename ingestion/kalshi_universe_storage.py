"""Write a Kalshi market-universe snapshot to the S3 raw zone as Parquet.

Layout::

    s3://<bucket>/raw/kalshi_universe/dt=YYYY-MM-DD/snapshot.parquet

One file per ``dt`` (the UTC date of the snapshot), overwritten on re-run, so a
daily snapshot is idempotent (last-write-wins). ``dt`` lives in the S3 key
(Hive-style), not the Parquet columns; Athena derives it via partition projection.
``PARQUET_SCHEMA`` is the explicit contract with the Glue external-table DDL
(``docs/setup/08-kalshi-universe.md``) — keep the column order + types in sync by hand.

``local_dir`` is an offline escape hatch: point it at a directory to write the same
``dt=`` layout on the local filesystem with NO AWS creds, so the whole ingest can be
built and verified before promoting to S3/Athena.
"""

from __future__ import annotations

import io
import logging
import os
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

if TYPE_CHECKING:  # avoid a circular import — kalshi_universe imports write_universe_to_s3
    from ingestion.kalshi_universe import UniverseRow

S3Client = Any

logger = logging.getLogger(__name__)

RAW_PREFIX = "raw/kalshi_universe"

# Column ORDER == the Glue CREATE EXTERNAL TABLE order; keep synced by hand (docs/setup/08).
PARQUET_SCHEMA = pa.schema(
    [
        ("snapshot_at", pa.timestamp("us", tz="UTC")),  # capture time (grain #1)
        ("market_ticker", pa.string()),  # grain #2
        ("event_ticker", pa.string()),
        ("series_ticker", pa.string()),
        ("category", pa.string()),
        ("status", pa.string()),
        ("open_time", pa.timestamp("us", tz="UTC")),
        ("close_time", pa.timestamp("us", tz="UTC")),
        ("yes_bid", pa.float64()),  # yes_bid_dollars (0..1)
        ("yes_ask", pa.float64()),  # yes_ask_dollars (0..1)
        ("volume_24h", pa.float64()),  # contracts (volume_24h_fp -> volume_fp fallback)
        ("open_interest", pa.float64()),  # contracts
        ("liquidity", pa.float64()),  # DOLLAR notional of resting book (depth proxy)
        ("yes_bid_size", pa.float64()),  # contracts at best bid
        ("yes_ask_size", pa.float64()),  # contracts at best ask
        ("fee_type", pa.string()),  # /series fee_type; 'quadratic_with_maker_fees' = maker-taxed
        ("ingested_at", pa.timestamp("us", tz="UTC")),  # ALWAYS last
    ]
)


def _require_bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set — copy .env.example to .env and fill it in")
    return bucket


def _rows_to_table(rows: list[UniverseRow], ingested_at: datetime) -> pa.Table:
    frame = pd.DataFrame(
        {
            "snapshot_at": pd.to_datetime([r.snapshot_at for r in rows], utc=True),
            "market_ticker": [r.market_ticker for r in rows],
            "event_ticker": [r.event_ticker for r in rows],
            "series_ticker": [r.series_ticker for r in rows],
            "category": [r.category for r in rows],
            "status": [r.status for r in rows],
            "open_time": pd.to_datetime([r.open_time for r in rows], utc=True),
            "close_time": pd.to_datetime([r.close_time for r in rows], utc=True),
            "yes_bid": [r.yes_bid for r in rows],
            "yes_ask": [r.yes_ask for r in rows],
            "volume_24h": [r.volume_24h for r in rows],
            "open_interest": [r.open_interest for r in rows],
            "liquidity": [r.liquidity for r in rows],
            "yes_bid_size": [r.yes_bid_size for r in rows],
            "yes_ask_size": [r.yes_ask_size for r in rows],
            "fee_type": [r.fee_type for r in rows],
            "ingested_at": pd.to_datetime([ingested_at] * len(rows), utc=True),
        }
    )
    return pa.Table.from_pandas(frame, schema=PARQUET_SCHEMA, preserve_index=False)


def _write_local(
    groups: dict[date, list[UniverseRow]], ingested_at: datetime, local_dir: str
) -> int:
    """Offline path: write the same dt= layout to the local filesystem (no AWS)."""
    written = 0
    for day, day_rows in sorted(groups.items()):
        table = _rows_to_table(day_rows, ingested_at)
        out = Path(local_dir) / RAW_PREFIX / f"dt={day.isoformat()}" / "snapshot.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, out.as_posix(), compression="snappy")  # type: ignore[no-untyped-call]
        written += 1
        logger.info("wrote %s (%d rows)", out, len(day_rows))
    return written


def write_universe_to_s3(
    rows: list[UniverseRow],
    *,
    s3_client: S3Client | None = None,
    ingested_at: datetime | None = None,
    local_dir: str | None = None,
) -> int:
    """Group rows by the UTC date of ``snapshot_at`` and write one Parquet per dt.

    With ``local_dir`` set, writes to the local filesystem instead of S3 (offline
    build path, no creds). Returns the number of partition files written.
    """
    if not rows:
        logger.info("no universe rows to write")
        return 0

    ingested_at = ingested_at or datetime.now(UTC)
    groups: dict[date, list[UniverseRow]] = defaultdict(list)
    for r in rows:
        groups[r.snapshot_at.date()].append(r)

    if local_dir is not None:
        return _write_local(groups, ingested_at, local_dir)

    s3 = s3_client or boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    bucket = _require_bucket()
    written = 0
    for day, day_rows in sorted(groups.items()):
        table = _rows_to_table(day_rows, ingested_at)
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
        key = f"{RAW_PREFIX}/dt={day.isoformat()}/snapshot.parquet"
        s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
        written += 1
        logger.info("wrote s3://%s/%s (%d rows)", bucket, key, len(day_rows))

    logger.info("wrote %d partition file(s) to s3://%s/%s", written, bucket, RAW_PREFIX)
    return written
