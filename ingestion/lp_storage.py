"""Land the LP market-making bot's logs into the S3 raw zone as date-partitioned Parquet.

Layout::

    s3://<bucket>/raw/lp_sessions/dt=YYYY-MM-DD/sessions.parquet
    s3://<bucket>/raw/lp_fills/dt=YYYY-MM-DD/fills.parquet

The bot (``core/maker/lp_live.py``) appends one row per market-session to ``data/lp_sessions.csv``
and one row per fill (with its markout) to ``data/lp_fills.csv``. This module lifts those
local CSVs into the same raw zone the rest of the platform uses, partitioned by the UTC
date of the session timestamp. ``dt`` lives in the S3 key (Hive-style), *not* in the Parquet
columns — Athena derives it via partition projection (see ``docs/setup/07-lp-pipeline.md``).
One file per ``dt``, overwritten on re-run, so re-ingests are idempotent.

Each ``*_SCHEMA`` is the explicit contract with the Glue external-table DDL — if a column
or type changes here, change it there too. Raw columns stay close to the CSV source; the
SQL-friendly renames happen in dbt staging (``stg_lp_*``).

Usage::

    uv run python -m ingestion.lp_storage                      # land both CSVs to S3
    uv run python -m ingestion.lp_storage --sessions-only
"""

from __future__ import annotations

import argparse
import io
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import boto3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

S3Client = Any  # boto3 clients ship no real type; Any is the idiomatic path

logger = logging.getLogger(__name__)

SESSIONS_CSV = "data/lp_sessions.csv"
FILLS_CSV = "data/lp_fills.csv"
SESSIONS_PREFIX = "raw/lp_sessions"
FILLS_PREFIX = "raw/lp_fills"

# Explicit schemas = the contract with the Glue CREATE EXTERNAL TABLE DDL.
SESSIONS_SCHEMA = pa.schema(
    [
        ("session_at", pa.timestamp("us", tz="UTC")),  # run/market start; join key to fills
        ("market_ticker", pa.string()),
        ("minutes", pa.float64()),
        ("n_fills", pa.int64()),
        ("fills_per_min", pa.float64()),
        ("net_cash", pa.float64()),  # spread capture (the repeatable edge)
        ("kalshi_gross", pa.float64()),  # realized incl. residual settlement
        ("fees", pa.float64()),
        ("max_abs_inv", pa.float64()),
        ("pnl_min", pa.float64()),
        ("pnl_max", pa.float64()),
        ("avg_spread_c", pa.float64()),
        ("mean_markout_c", pa.float64()),
        ("config_version", pa.string()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)

FILLS_SCHEMA = pa.schema(
    [
        ("session_at", pa.timestamp("us", tz="UTC")),  # joins to sessions.session_at + market
        ("market_ticker", pa.string()),
        ("fill_at", pa.timestamp("us", tz="UTC")),  # exchange fill time
        ("book_side", pa.string()),  # bid / ask (which side we were resting)
        ("price", pa.float64()),
        ("mid_at_fill", pa.float64()),
        ("mid_after", pa.float64()),  # mid at fill + markout horizon
        ("markout_c", pa.float64()),
        ("ingested_at", pa.timestamp("us", tz="UTC")),
    ]
)


def _require_bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("S3_BUCKET is not set — copy .env.example to .env and fill it in")
    return bucket


def load_sessions(path: str = SESSIONS_CSV) -> pd.DataFrame:
    """Read the sessions CSV into a typed frame matching SESSIONS_SCHEMA (minus partition)."""
    df = pd.read_csv(path)
    out = pd.DataFrame(
        {
            "session_at": pd.to_datetime(df["ts_utc"], utc=True),
            "market_ticker": df["market"].astype("string"),
            "minutes": df["minutes"].astype("float64"),
            "n_fills": df["n_fills"].astype("int64"),
            "fills_per_min": df["fills_per_min"].astype("float64"),
            "net_cash": df["net_cash"].astype("float64"),
            "kalshi_gross": df["kalshi_gross"].astype("float64"),
            "fees": df["fees"].astype("float64"),
            "max_abs_inv": df["max_abs_inv"].astype("float64"),
            "pnl_min": df["pnl_min"].astype("float64"),
            "pnl_max": df["pnl_max"].astype("float64"),
            "avg_spread_c": df["avg_spread_c"].astype("float64"),
            "mean_markout_c": pd.to_numeric(df["mean_markout_c"], errors="coerce"),
            "config_version": df.get("config_version", "legacy").astype("string"),
        }
    )
    return out


def load_fills(path: str = FILLS_CSV) -> pd.DataFrame:
    """Read the fills CSV into a typed frame matching FILLS_SCHEMA (minus partition)."""
    df = pd.read_csv(path)
    return pd.DataFrame(
        {
            "session_at": pd.to_datetime(df["ts_utc"], utc=True),
            "market_ticker": df["market"].astype("string"),
            "fill_at": pd.to_datetime(df["fill_ts"], unit="s", utc=True),
            "book_side": df["side"].astype("string"),
            "price": df["price"].astype("float64"),
            "mid_at_fill": df["mid0"].astype("float64"),
            "mid_after": df["mid_h"].astype("float64"),
            "markout_c": pd.to_numeric(df["markout_c"], errors="coerce"),
        }
    )


def write_partitioned(
    df: pd.DataFrame,
    prefix: str,
    schema: pa.Schema,
    filename: str,
    *,
    s3_client: S3Client | None = None,
    ingested_at: datetime | None = None,
) -> int:
    """Group rows by the UTC date of ``session_at`` and write one Parquet object per day
    to ``s3://<bucket>/<prefix>/dt=YYYY-MM-DD/<filename>``. Overwrites each partition, so
    re-runs are idempotent. Returns the number of partition files written."""
    if df.empty:
        logger.info("no rows for %s", prefix)
        return 0
    s3 = s3_client or boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    bucket = _require_bucket()
    df = df.assign(ingested_at=ingested_at or datetime.now(UTC))

    written = 0
    for day, day_df in df.groupby(df["session_at"].dt.date):
        table = pa.Table.from_pandas(day_df, schema=schema, preserve_index=False)
        buffer = io.BytesIO()
        pq.write_table(table, buffer, compression="snappy")  # type: ignore[no-untyped-call]
        key = f"{prefix}/dt={day.isoformat()}/{filename}"
        s3.put_object(Bucket=bucket, Key=key, Body=buffer.getvalue())
        written += 1
        logger.info("wrote s3://%s/%s (%d rows)", bucket, key, len(day_df))
    logger.info("wrote %d partition file(s) to s3://%s/%s", written, bucket, prefix)
    return written


def ingest(
    *,
    sessions: bool = True,
    fills: bool = True,
    s3_client: S3Client | None = None,
) -> dict[str, int]:
    """Land the configured CSVs to S3. Returns {dataset: partition_files_written}."""
    ingested_at = datetime.now(UTC)
    out: dict[str, int] = {}
    if sessions:
        out["lp_sessions"] = write_partitioned(
            load_sessions(SESSIONS_CSV), SESSIONS_PREFIX, SESSIONS_SCHEMA, "sessions.parquet",
            s3_client=s3_client, ingested_at=ingested_at,
        )
    if fills:
        out["lp_fills"] = write_partitioned(
            load_fills(FILLS_CSV), FILLS_PREFIX, FILLS_SCHEMA, "fills.parquet",
            s3_client=s3_client, ingested_at=ingested_at,
        )
    return out


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Land LP bot logs to the S3 raw zone")
    ap.add_argument("--sessions-only", action="store_true")
    ap.add_argument("--fills-only", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    do_sessions = not args.fills_only
    do_fills = not args.sessions_only
    counts = ingest(sessions=do_sessions, fills=do_fills)
    for ds, n in counts.items():
        print(f"{ds}: {n} partition file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
