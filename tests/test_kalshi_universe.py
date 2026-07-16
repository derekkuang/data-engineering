"""Unit tests for the Kalshi universe snapshot — normalization/filtering, the fee_type
series cache, the dt= partition layout, the Parquet schema contract, and the offline
local_dir path. No AWS: a FakeS3 captures put_object and a FakeClient stubs the API."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from ingestion import kalshi_universe as ku
from ingestion import kalshi_universe_storage as store

# One open event page with a mix of markets: a good two-sided row, a one-sided row (no ask),
# a crossed row (ask<=bid), a zero-volume row, and a row that must fall back volume_fp->24h.
EVENTS_PAGE = {
    "events": [
        {
            "series_ticker": "KXWCSPREAD",
            "category": "Sports",
            "event_ticker": "KXWCSPREAD-26JUL15ABC",
            "markets": [
                {
                    "ticker": "KXWCSPREAD-26JUL15ABC-A",
                    "status": "active",
                    "open_time": "2026-07-15T14:00:00Z",
                    "close_time": "2026-07-15T16:00:00Z",
                    "yes_bid_dollars": "0.58",
                    "yes_ask_dollars": "0.60",
                    "volume_24h_fp": "5000",
                    "open_interest_fp": "3200",
                    "liquidity_dollars": "1234.5",
                    "yes_bid_size_fp": "40",
                    "yes_ask_size_fp": "55",
                },
                {  # one-sided (no ask) -> dropped
                    "ticker": "KXWCSPREAD-26JUL15ABC-B",
                    "yes_bid_dollars": "0.20",
                    "volume_24h_fp": "900",
                },
                {  # crossed (ask <= bid) -> dropped
                    "ticker": "KXWCSPREAD-26JUL15ABC-C",
                    "yes_bid_dollars": "0.60",
                    "yes_ask_dollars": "0.50",
                    "volume_24h_fp": "900",
                },
                {  # zero volume -> dropped
                    "ticker": "KXWCSPREAD-26JUL15ABC-D",
                    "yes_bid_dollars": "0.40",
                    "yes_ask_dollars": "0.45",
                    "volume_24h_fp": "0",
                },
                {  # no volume_24h_fp -> must fall back to volume_fp; survives
                    "ticker": "KXWCSPREAD-26JUL15ABC-E",
                    "status": "active",
                    "yes_bid_dollars": "0.30",
                    "yes_ask_dollars": "0.34",
                    "volume_fp": "1500",
                },
            ],
        }
    ],
    "cursor": None,
}


class FakeClient:
    """Stubs the two endpoints the snapshot uses, plus close()."""

    def __init__(self, fee_type: str | None = "quadratic_with_maker_fees") -> None:
        self.fee_type = fee_type
        self.list_series_calls = 0

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert path == "/events"
        return EVENTS_PAGE

    def list_series(self, category: str | None = None) -> list[dict[str, Any]]:
        self.list_series_calls += 1
        return [{"ticker": "KXWCSPREAD", "fee_type": self.fee_type, "category": "Sports"}]

    def close(self) -> None:
        pass


class FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.puts.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _rows() -> list[ku.UniverseRow]:
    client = FakeClient()
    return ku.fetch_universe_rows(client, snapshot_at=datetime(2026, 7, 15, 12, tzinfo=UTC))


def test_fetch_filters_and_fee_type() -> None:
    rows = _rows()
    tickers = {r.market_ticker for r in rows}
    # only the good two-sided row and the volume_fp-fallback row survive
    assert tickers == {"KXWCSPREAD-26JUL15ABC-A", "KXWCSPREAD-26JUL15ABC-E"}
    good = next(r for r in rows if r.market_ticker.endswith("-A"))
    assert good.yes_bid == 0.58 and good.yes_ask == 0.60
    assert good.volume_24h == 5000.0 and good.open_interest == 3200.0
    assert good.liquidity == 1234.5 and good.yes_bid_size == 40.0 and good.yes_ask_size == 55.0
    assert good.series_ticker == "KXWCSPREAD" and good.category == "Sports"
    assert good.fee_type == "quadratic_with_maker_fees"  # from the cached /series fan-out
    fallback = next(r for r in rows if r.market_ticker.endswith("-E"))
    assert fallback.volume_24h == 1500.0  # volume_24h_fp absent -> volume_fp used


def test_fee_type_from_single_list_call() -> None:
    client = FakeClient()
    ku.fetch_universe_rows(client, snapshot_at=datetime(2026, 7, 15, 12, tzinfo=UTC))
    assert client.list_series_calls == 1  # whole fee_type map from ONE call, no per-series fan-out


def _row(day: str, ticker: str) -> ku.UniverseRow:
    return ku.UniverseRow(
        snapshot_at=datetime.fromisoformat(f"{day}T12:00:00+00:00"),
        market_ticker=ticker, event_ticker="E", series_ticker="KXWCSPREAD", category="Sports",
        status="active", open_time=None, close_time=None,
        yes_bid=0.58, yes_ask=0.60, volume_24h=5000.0, open_interest=3200.0,
        liquidity=1234.5, yes_bid_size=40.0, yes_ask_size=55.0, fee_type="quadratic",
    )


def test_write_partitioned_by_utc_day(monkeypatch: Any) -> None:
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    rows = [_row("2026-07-15", "M1"), _row("2026-07-16", "M2")]
    fake = FakeS3()
    n = store.write_universe_to_s3(rows, s3_client=fake)
    assert n == 2
    keys = sorted(p["Key"] for p in fake.puts)
    assert keys == [
        "raw/kalshi_universe/dt=2026-07-15/snapshot.parquet",
        "raw/kalshi_universe/dt=2026-07-16/snapshot.parquet",
    ]
    table = pq.read_table(io.BytesIO(fake.puts[0]["Body"]))
    assert table.schema.equals(store.PARQUET_SCHEMA)  # the raw-table contract
    assert table.num_rows == 1
    assert table.column("ingested_at")[0].as_py() is not None


def test_write_local_dir(tmp_path: Any) -> None:
    fake = FakeS3()
    n = store.write_universe_to_s3(
        [_row("2026-07-15", "M1")], s3_client=fake, local_dir=str(tmp_path)
    )
    assert n == 1 and fake.puts == []  # local path never touches S3
    out = Path(tmp_path) / "raw/kalshi_universe/dt=2026-07-15/snapshot.parquet"
    assert out.exists()
    # read the bytes (not the path) so pyarrow doesn't append the dt= Hive partition column
    assert pq.read_table(io.BytesIO(out.read_bytes())).schema.equals(store.PARQUET_SCHEMA)


def test_ingest_snapshot_summary(monkeypatch: Any) -> None:
    monkeypatch.setenv("S3_BUCKET", "test-bucket")
    fake = FakeS3()
    summary = ku.ingest_snapshot(s3_client=fake, client=FakeClient())
    assert summary == {"series": 1, "markets": 2, "files": 1}
