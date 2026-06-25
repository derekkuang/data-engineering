"""Unit tests for ingestion.lp_storage — parsing, partitioning, and the Parquet
schema contract, all without touching AWS (a fake S3 client captures put_object)."""

from __future__ import annotations

import io
from typing import Any

import pyarrow.parquet as pq

from ingestion import lp_storage

SESSIONS_CSV = (
    "ts_utc,market,minutes,n_fills,fills_per_min,net_cash,kalshi_gross,fees,"
    "max_abs_inv,pnl_min,pnl_max,avg_spread_c,mean_markout_c,config_version\n"
    "2026-06-21T23:35:44+00:00,KXWCSPREAD-A,10.0,40,4.0,1.50,1.80,0.03,2,0.0,2.0,3.6,-0.25,2026-06-18-meanrev\n"
    "2026-06-22T01:10:00+00:00,KXMLBTOTAL-B,5.0,12,2.4,0.40,0.40,0.00,1,-0.1,0.5,2.9,nan,2026-06-21-2x\n"
)

FILLS_CSV = (
    "ts_utc,market,fill_ts,side,price,mid0,mid_h,markout_c\n"
    "2026-06-21T23:35:44+00:00,KXWCSPREAD-A,1781652342,bid,0.62,0.67,0.795,12.5\n"
    "2026-06-22T01:10:00+00:00,KXMLBTOTAL-B,1781659800,ask,0.48,0.48,0.48,0.0\n"
)


class FakeS3:
    """Captures put_object calls so we can assert keys and round-trip the Parquet bodies."""

    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _write(tmp_path: Any, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_load_sessions_types(tmp_path: Any) -> None:
    df = lp_storage.load_sessions(_write(tmp_path, "s.csv", SESSIONS_CSV))
    assert list(df["market_ticker"]) == ["KXWCSPREAD-A", "KXMLBTOTAL-B"]
    assert df["n_fills"].dtype == "int64"
    dt = str(df["session_at"].dtype)
    assert dt.startswith("datetime64") and "UTC" in dt
    # "nan" markout must parse to a real NaN, not a string
    assert df["mean_markout_c"].isna().tolist() == [False, True]


def test_load_fills_unix_to_timestamp(tmp_path: Any) -> None:
    df = lp_storage.load_fills(_write(tmp_path, "f.csv", FILLS_CSV))
    assert list(df["book_side"]) == ["bid", "ask"]
    # fill_ts 1781652342 (unix s) must become a 2026 UTC timestamp
    assert df["fill_at"].iloc[0].year == 2026
    assert df["mid_at_fill"].iloc[0] == 0.67


def test_write_partitioned_by_utc_day(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    df = lp_storage.load_sessions(_write(tmp_path, "s.csv", SESSIONS_CSV))
    fake = FakeS3()
    n = lp_storage.write_partitioned(
        df, lp_storage.SESSIONS_PREFIX, lp_storage.SESSIONS_SCHEMA, "sessions.parquet",
        s3_client=fake,
    )
    # two rows on two distinct UTC days -> two partition files
    assert n == 2
    keys = sorted(p["Key"] for p in fake.puts)
    assert keys == [
        "raw/lp_sessions/dt=2026-06-21/sessions.parquet",
        "raw/lp_sessions/dt=2026-06-22/sessions.parquet",
    ]
    # round-trip: the Parquet body matches the schema contract + carries ingested_at
    table = pq.read_table(io.BytesIO(fake.puts[0]["Body"]))
    assert table.schema.equals(lp_storage.SESSIONS_SCHEMA)
    assert table.num_rows == 1


def test_ingest_routes_both_datasets(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    monkeypatch.setattr(lp_storage, "SESSIONS_CSV", _write(tmp_path, "s.csv", SESSIONS_CSV))
    monkeypatch.setattr(lp_storage, "FILLS_CSV", _write(tmp_path, "f.csv", FILLS_CSV))
    fake = FakeS3()
    out = lp_storage.ingest(s3_client=fake)
    assert out == {"lp_sessions": 2, "lp_fills": 2}
