"""Snapshot the Kalshi market universe and land it in the S3 raw zone.

A daily point-in-time snapshot of every OPEN market with a two-sided quote and real
24h volume — the spread/depth/volume/fee_type landscape that the LP-opportunity mart
(``fct_kalshi_opportunity``) ranks. Public data, no auth. One Parquet per ``dt``
(the UTC snapshot date), overwritten on re-run — daily last-snapshot-wins, idempotent.

The universe is enumerated the way ``core/maker/lp_market_screen.py`` does it — paginate
``/events?with_nested_markets=true`` (events carry series_ticker + category and exclude
the auto-generated MVE parlay spam) — plus a cached ``/series`` fan-out for each distinct
series' ``fee_type`` (the maker-fee flag). It never fans out per-market order books
(universe-scale orderbook calls would take minutes-to-hours); top-of-book depth comes
from the snapshot fields (``liquidity_dollars`` + best-bid/ask sizes).

Usage::

    uv run python -m ingestion.kalshi_universe                  # land to S3 (needs S3_BUCKET)
    uv run python -m ingestion.kalshi_universe --dry-run        # fetch + field diagnostic, no write
    uv run python -m ingestion.kalshi_universe --local-dir ./wh # write dt= Parquet locally (no AWS)
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import KalshiClient, _parse_iso
from ingestion.kalshi_universe_storage import write_universe_to_s3

logger = logging.getLogger(__name__)

EVENTS_PAGE_LIMIT = 200
MAX_PAGES = 200  # safety cap on pagination
MIN_VOLUME_24H = 1.0  # ignore markets with no flow — nothing to capture, mirrors lp_market_screen


@dataclass(frozen=True)
class UniverseRow:
    """One open market's snapshot — field order/names mirror the raw PARQUET_SCHEMA."""

    snapshot_at: datetime
    market_ticker: str
    event_ticker: str
    series_ticker: str
    category: str
    status: str
    open_time: datetime | None
    close_time: datetime | None
    yes_bid: float
    yes_ask: float
    volume_24h: float
    open_interest: float
    liquidity: float
    yes_bid_size: float | None
    yes_ask_size: float | None
    fee_type: str | None


def _f(d: dict[str, Any], *keys: str) -> float | None:
    """First present key parsed to float (handles the _fp -> fallback keys), else None."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _iso_or_none(s: str | None) -> datetime | None:
    return _parse_iso(s) if s else None


def fetch_open_events(client: KalshiClient) -> list[dict[str, Any]]:
    """Paginate open events WITH nested markets — the clean entry to the real universe
    (carries series_ticker + category, excludes the MVE parlay spam). Verbatim pattern
    from core/maker/lp_market_screen.py."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        p: dict[str, Any] = {
            "status": "open",
            "with_nested_markets": "true",
            "limit": EVENTS_PAGE_LIMIT,
        }
        if cursor:
            p["cursor"] = cursor
        resp = client.get("/events", params=p)
        out.extend(resp.get("events", []))
        cursor = resp.get("cursor") or None
        if not cursor:
            break
    return out


def _fee_type_map(client: KalshiClient) -> dict[str, str | None]:
    """{series_ticker: fee_type} for the whole universe in ONE /series call. fee_type is a
    near-static series property, so a single list call beats a per-series fan-out (which was
    thousands of paced requests). Returns {} on failure — fee_type just lands null."""
    try:
        return {s["ticker"]: s.get("fee_type") for s in client.list_series() if s.get("ticker")}
    except Exception as exc:  # noqa: BLE001 — a fee_type outage must not sink the whole snapshot
        logger.warning("series list fetch failed, fee_type will be null: %s", str(exc)[:80])
        return {}


def fetch_universe_rows(client: KalshiClient, *, snapshot_at: datetime) -> list[UniverseRow]:
    """One UniverseRow per open market with a two-sided quote and real 24h volume,
    tagged with its event's series/category and its series' fee_type."""
    events = fetch_open_events(client)
    fee_types = _fee_type_map(client)

    rows: list[UniverseRow] = []
    for e in events:
        series = e.get("series_ticker") or e.get("event_ticker", "").split("-")[0]
        category = e.get("category") or "Unknown"
        event_ticker = e.get("event_ticker", "")
        for m in e.get("markets") or []:
            bid, ask = _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars")
            vol = _f(m, "volume_24h_fp", "volume_fp") or 0.0
            if bid is None or ask is None or ask <= bid or vol < MIN_VOLUME_24H:
                continue
            rows.append(
                UniverseRow(
                    snapshot_at=snapshot_at,
                    market_ticker=m["ticker"],
                    event_ticker=event_ticker,
                    series_ticker=series,
                    category=category,
                    status=str(m.get("status") or ""),
                    open_time=_iso_or_none(m.get("open_time")),
                    close_time=_iso_or_none(m.get("close_time")),
                    yes_bid=bid,
                    yes_ask=ask,
                    volume_24h=vol,
                    open_interest=_f(m, "open_interest_fp") or 0.0,
                    liquidity=_f(m, "liquidity_dollars") or 0.0,
                    yes_bid_size=_f(m, "yes_bid_size_fp"),
                    yes_ask_size=_f(m, "yes_ask_size_fp"),
                    fee_type=fee_types.get(series),
                )
            )
    return rows


def ingest_snapshot(
    *,
    s3_client: Any = None,
    local_dir: str | None = None,
    client: KalshiClient | None = None,
) -> dict[str, int]:
    """Fetch the open-universe snapshot and land it. Returns a small XCom-friendly summary."""
    load_dotenv()
    owned = client is None
    client = client or KalshiClient(pace_seconds=0.2)
    snapshot_at = datetime.now(UTC)
    try:
        rows = fetch_universe_rows(client, snapshot_at=snapshot_at)
    finally:
        if owned:
            client.close()
    files = write_universe_to_s3(rows, s3_client=s3_client, local_dir=local_dir)
    return {
        "series": len({r.series_ticker for r in rows}),
        "markets": len(rows),
        "files": files,
    }


def _dry_run() -> int:
    """Fetch + print a RAW field diagnostic and a qualifying-row count (no write) — confirms
    the API field names the schema depends on. Deliberately does ONE universe pagination and
    a single sample /series call; the full per-series fee_type fan-out is skipped here (it
    doesn't affect the filter and would add hundreds of paced calls)."""
    load_dotenv()
    client = KalshiClient(pace_seconds=0.2)
    try:
        events = fetch_open_events(client)
        n_markets = sum(len(e.get("markets") or []) for e in events)
        print(f"open events: {len(events)}   nested markets: {n_markets}")
        sample_market = next((m for e in events for m in (e.get("markets") or [])), None)
        if sample_market:
            print("\nsample market raw keys:")
            for k in sorted(sample_market):
                print(f"  {k}: {str(sample_market[k])[:60]}")
        sample_series = next((e["series_ticker"] for e in events if e.get("series_ticker")), None)
        if sample_series:
            print(f"\n/series/{sample_series} payload:")
            payload = client.get_series(sample_series)
            for k in sorted(payload):
                print(f"  {k}: {str(payload[k])[:60]}")
        qualifying = 0
        series_seen: set[str] = set()
        for e in events:
            s = e.get("series_ticker") or e.get("event_ticker", "").split("-")[0]
            for m in e.get("markets") or []:
                bid, ask = _f(m, "yes_bid_dollars"), _f(m, "yes_ask_dollars")
                vol = _f(m, "volume_24h_fp", "volume_fp") or 0.0
                if bid is None or ask is None or ask <= bid or vol < MIN_VOLUME_24H:
                    continue
                qualifying += 1
                series_seen.add(s)
        print(f"\nqualifying rows (two-sided + vol>= {MIN_VOLUME_24H:g}): {qualifying} "
              f"across {len(series_seen)} series (fee_type fan-out skipped in --dry-run)")
    finally:
        client.close()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Snapshot the Kalshi market universe to the raw zone")
    ap.add_argument("--dry-run", action="store_true", help="fetch + field diagnostic, no write")
    ap.add_argument("--local-dir", default=None, help="write dt= Parquet here, not S3 (no AWS)")
    args = ap.parse_args()
    if args.dry_run:
        return _dry_run()
    summary = ingest_snapshot(local_dir=args.local_dir)
    print(f"snapshot: {summary['markets']} markets / {summary['series']} series "
          f"-> {summary['files']} partition file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
