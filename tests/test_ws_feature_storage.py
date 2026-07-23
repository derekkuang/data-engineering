"""Unit tests for ingestion.ws_feature_storage — CSV typing, dt= partitioning, and the
Parquet schema contract, without touching AWS (a fake S3 captures put_object)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from ingestion import ws_feature_storage as wfs

FEATURES_CSV = (
    "ts_utc,market,bid,ask,mid,spread_c,imbalance,yes_depth,no_depth,depth_near,n_levels,"
    "trades_1m,vol_1m,taker_buy_frac,signed_flow_1m,midvol_1m,midmove_1m\n"
    "2026-07-18T18:00:00+00:00,KXWCTOTAL-A,0.58,0.60,0.59,2.0,0.10,40,32,60,6,5,120,0.6,8,0.4,0.2\n"
    "2026-07-19T01:30:00+00:00,KXLIGAMXTOTAL-B,0.30,0.34,0.32,4.0,-0.20,20,30,25,4,2,40,0.4,-6,0.9,-0.5\n"
)


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _csv(tmp_path: Any) -> str:
    p = tmp_path / "ws_features.csv"
    p.write_text(FEATURES_CSV)
    return str(p)


def test_load_features_types(tmp_path: Any) -> None:
    df = wfs.load_features(_csv(tmp_path))
    assert list(df["market_ticker"]) == ["KXWCTOTAL-A", "KXLIGAMXTOTAL-B"]
    dt = str(df["snapshot_at"].dtype)
    assert dt.startswith("datetime64") and "UTC" in dt
    assert df["signed_flow_1m"].tolist() == [8.0, -6.0]


def test_write_partitioned_by_utc_day(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    df = wfs.load_features(_csv(tmp_path))
    fake = FakeS3()
    landed = datetime(2026, 7, 19, 3, 15, 42, tzinfo=UTC)
    n = wfs.write_features(df, s3_client=fake, ingested_at=landed)
    assert n == 2  # two distinct UTC days
    keys = sorted(p["Key"] for p in fake.puts)
    # filename carries the LANDING time: same-day captures accumulate, never clobber
    assert keys == [
        "raw/ws_features/dt=2026-07-18/features-031542.parquet",
        "raw/ws_features/dt=2026-07-19/features-031542.parquet",
    ]
    table = pq.read_table(io.BytesIO(fake.puts[0]["Body"]))
    assert table.schema.equals(wfs.FEATURES_SCHEMA)
    assert table.column("ingested_at")[0].as_py() is not None


def test_second_landing_same_day_does_not_clobber(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    df = wfs.load_features(_csv(tmp_path))
    fake = FakeS3()
    wfs.write_features(df, s3_client=fake, ingested_at=datetime(2026, 7, 19, 1, 0, tzinfo=UTC))
    wfs.write_features(df, s3_client=fake, ingested_at=datetime(2026, 7, 19, 4, 0, tzinfo=UTC))
    day_keys = {p["Key"] for p in fake.puts if "dt=2026-07-18" in p["Key"]}
    assert len(day_keys) == 2  # two distinct objects for the same dt partition


def test_write_local_dir(tmp_path: Any) -> None:
    df = wfs.load_features(_csv(tmp_path))
    fake = FakeS3()
    landed = datetime(2026, 7, 19, 3, 15, 42, tzinfo=UTC)
    n = wfs.write_features(df, s3_client=fake, local_dir=str(tmp_path / "wh"), ingested_at=landed)
    assert n == 2 and fake.puts == []  # local path never touches S3
    out = Path(tmp_path) / "wh" / "raw/ws_features/dt=2026-07-18/features-031542.parquet"
    assert out.exists()
    assert pq.read_table(io.BytesIO(out.read_bytes())).schema.equals(wfs.FEATURES_SCHEMA)


def test_ingest_idle_window_is_a_green_noop(tmp_path: Any, monkeypatch: Any) -> None:
    """An idle capture (no live markets) writes no CSV — landing must succeed as a no-op,
    not crash the workflow red with FileNotFoundError."""
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    fake = FakeS3()
    n = wfs.ingest(s3_client=fake, path=str(tmp_path / "does_not_exist.csv"))
    assert n == 0 and fake.puts == []
