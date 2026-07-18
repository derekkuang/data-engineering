"""Land the WebSocket microstructure-feature capture into the S3 raw zone as Parquet.

Layout::

    s3://<bucket>/raw/ws_features/dt=YYYY-MM-DD/features.parquet

``ml/lp/ws_features.py`` captures, per market per snapshot, the pre-quote microstructure
features (book shape + imbalance, trade flow, mid vol/drift) to ``data/ws_features.csv``.
This lifts that CSV into the same raw zone the rest of the platform uses, partitioned by the
UTC date of the snapshot. ``dt`` lives in the S3 key (Hive-style), not the Parquet columns —
Athena derives it via partition projection. One file per ``dt``, overwritten on re-run, so
re-ingests are idempotent.

``FEATURES_SCHEMA`` is the explicit contract with the Glue external-table DDL
(``docs/setup/09-ws-capture.md``) — keep the column order + types in sync by hand. This is the
feature store the toxicity/selection model trains on, and the source for the markout label
(``fct_ws_markout`` joins each snapshot forward to a later mid).

Usage::

    uv run python -m ingestion.ws_feature_storage                 # land data/ws_features.csv
    uv run python -m ingestion.ws_feature_storage --local-dir ./wh  # offline (no AWS)
"""

from __future__ import annotations

import argparse
import io
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

S3Client = Any

logger = logging.getLogger(__name__)

FEATURES_CSV = "data/ws_features.csv"
RAW_PREFIX = "raw/ws_features"

# Explicit schema = the contract with the Glue CREATE EXTERNAL TABLE DDL (docs/setup/09).
FEATURES_SCHEMA = pa.schema(
    [
        ("snapshot_at", pa.timestamp("us", tz="UTC")),  # capture time (grain #1)
        ("market_ticker", pa.string()),  # grain #2
        ("bid", pa.float64()),
        ("ask", pa.float64()),
        ("mid", pa.float64()),
        ("spread_c", pa.float64()),
        ("imbalance", pa.float64()),  # (yes_depth - no_depth)/total in [-1,1]
        ("yes_depth", pa.float64()),
        ("no_depth", pa.float64()),
        ("depth_near", pa.float64()),  # contracts within 3c of the touch
        ("n_levels", pa.float64()),
        ("trades_1m", pa.float64()),
        ("vol_1m", pa.float64()),
        ("taker_buy_frac", pa.float64()),
        ("signed_flow_1m", pa.float64()),  # net taker direction (contracts); adverse-select signal
        ("midvol_1m", pa.float64()),  # realized mid vol over the window (cents)
        ("midmove_1m", pa.float64()),  # trailing mid drift (cents)
        ("ingested_at", pa.timestamp("us", tz="UTC")),  # ALWAYS last
    ]
)


def _require_bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set — copy .env.example to .env and fill it in")
    return bucket


def load_features(path: str = FEATURES_CSV) -> pd.DataFrame:
    """Read the feature CSV into a typed frame matching FEATURES_SCHEMA (minus partition)."""
    df = pd.read_csv(path)
    out = pd.DataFrame({"snapshot_at": pd.to_datetime(df["ts_utc"], utc=True, format="ISO8601")})
    out["market_ticker"] = df["market"].astype("string")
    for col in [
        "bid", "ask", "mid", "spread_c", "imbalance", "yes_depth", "no_depth", "depth_near",
        "n_levels", "trades_1m", "vol_1m", "taker_buy_frac", "signed_flow_1m", "midvol_1m",
        "midmove_1m",
    ]:
        out[col] = pd.to_numeric(df[col], errors="coerce")
    return out


def _to_table(df: pd.DataFrame, ingested_at: datetime) -> pa.Table:
    df = df.assign(ingested_at=ingested_at)
    return pa.Table.from_pandas(df, schema=FEATURES_SCHEMA, preserve_index=False)


def write_features(
    df: pd.DataFrame,
    *,
    s3_client: S3Client | None = None,
    ingested_at: datetime | None = None,
    local_dir: str | None = None,
) -> int:
    """Group by the UTC date of ``snapshot_at`` and write one Parquet per dt (S3, or a local
    dir for the offline path). Overwrites each partition. Returns partition files written."""
    if df.empty:
        logger.info("no feature rows to write")
        return 0
    ingested_at = ingested_at or datetime.now(UTC)
    s3 = None
    if local_dir is None:
        s3 = s3_client or boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        bucket = _require_bucket()

    written = 0
    for day, day_df in df.groupby(df["snapshot_at"].dt.date):
        table = _to_table(day_df, ingested_at)
        rel = f"{RAW_PREFIX}/dt={day.isoformat()}/features.parquet"
        if local_dir is not None:
            out = Path(local_dir) / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, out.as_posix(), compression="snappy")  # type: ignore[no-untyped-call]
        else:
            buffer = io.BytesIO()
            pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
            s3.put_object(Bucket=bucket, Key=rel, Body=buffer.getvalue())  # type: ignore[union-attr]
        written += 1
        logger.info("wrote %s (%d rows)", rel, len(day_df))
    logger.info("wrote %d partition file(s)", written)
    return written


def ingest(
    *, s3_client: S3Client | None = None, local_dir: str | None = None, path: str = FEATURES_CSV
) -> int:
    return write_features(load_features(path), s3_client=s3_client, local_dir=local_dir)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Land the WS microstructure features to the raw zone")
    ap.add_argument("--local-dir", default=None, help="write dt= Parquet here, not S3 (no AWS)")
    args = ap.parse_args()
    n = ingest(local_dir=args.local_dir)
    print(f"ws_features: {n} partition file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
