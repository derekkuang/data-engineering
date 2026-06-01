"""Ingest Kalshi 15-min BTC markets into the S3 raw zone.

Two entry points share one fetch loop:

- ``backfill(days=N)`` — historical *settled* markets over the last N days (CLI).
- ``ingest_current_day()`` — re-fetch the current UTC day's markets (settled +
  the in-progress window) and overwrite that day's partition. Idempotent, so the
  live Airflow task can call it every 15 minutes; settled windows pick up their
  final ``result``/candles as they close — same "re-fetch the day, overwrite"
  contract as the Coinbase ingest.

Lands one Parquet per day at ``s3://<bucket>/raw/kalshi_btc_15min/dt=YYYY-MM-DD/``.
Kalshi market data is public; no auth. Paced to respect the API rate limit.

CLI::

    uv run python -m ingestion.kalshi_backfill --days 1 --dry-run
    uv run python -m ingestion.kalshi_backfill --days 7
"""

from __future__ import annotations

import argparse
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from dotenv import load_dotenv

from ingestion.kalshi import (
    SERIES_BTC_15M,
    KalshiCandle,
    KalshiClient,
    _parse_iso,
    normalize_market_candles,
)
from ingestion.kalshi_storage import write_candles_to_s3

logger = logging.getLogger("kalshi_backfill")


# Kalshi caps "requested candlesticks across all markets" (= n_markets × minutes
# in the time range) at 10,000 per batch call. Keep a margin under that.
CANDLE_BUDGET = 9000


def _chunk_by_budget(day_markets: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Greedily group time-contiguous markets so n_markets × span_minutes stays
    under the batch budget (markets are sorted by open_time)."""
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for market in day_markets:
        trial = cur + [market]
        span_min = (
            _parse_iso(trial[-1]["close_time"]) - _parse_iso(trial[0]["open_time"])
        ).total_seconds() / 60 + 2
        if cur and len(trial) * span_min > CANDLE_BUDGET:
            chunks.append(cur)
            cur = [market]
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    return chunks


def _fetch_candles(
    client: KalshiClient, markets: list[dict[str, Any]], series: str
) -> list[KalshiCandle]:
    """Fetch + normalize candlesticks via the batch endpoint, grouped into
    time-contiguous chunks that respect the per-call candlestick budget — a few
    calls per day instead of ~96 per-market calls."""
    by_day: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for market in markets:
        try:
            by_day[_parse_iso(market["close_time"]).date()].append(market)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skipping market with bad close_time %r — %r", market.get("ticker"), exc)

    candles: list[KalshiCandle] = []
    for day, day_markets in sorted(by_day.items()):
        day_markets.sort(key=lambda m: m["open_time"])
        for chunk in _chunk_by_budget(day_markets):
            try:
                opens = [int(_parse_iso(m["open_time"]).timestamp()) for m in chunk]
                closes = [int(_parse_iso(m["close_time"]).timestamp()) for m in chunk]
                cs_by_ticker = client.get_market_candlesticks_batch(
                    [m["ticker"] for m in chunk],
                    min(opens) - 60,
                    max(closes) + 60,
                    period_interval=1,
                )
                for m in chunk:
                    cs = cs_by_ticker.get(m["ticker"], [])
                    candles.extend(normalize_market_candles(m, cs, series))
            except Exception as exc:  # noqa: BLE001
                logger.warning("skipping a chunk of %d markets on %s — %r", len(chunk), day, exc)
        logger.info("  %s: %d markets, %d candles cumulative", day, len(day_markets), len(candles))
    return candles


def backfill(days: int, series: str = SERIES_BTC_15M, *, dry_run: bool = False) -> int:
    """Backfill settled markets closing in the last ``days`` days."""
    load_dotenv()
    client = KalshiClient()
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    logger.info("listing settled %s markets closing %s .. %s", series,
                start.isoformat(timespec="minutes"), now.isoformat(timespec="minutes"))
    markets = client.list_markets(
        series, status="settled",
        min_close_ts=int(start.timestamp()), max_close_ts=int(now.timestamp()),
    )
    logger.info("found %d settled markets", len(markets))
    candles = _fetch_candles(client, markets, series)
    logger.info("normalized %d candles from %d markets", len(candles), len(markets))

    if dry_run:
        for c in candles[:3]:
            logger.info("sample: %s @ %s prob_close=%s bid/ask=%s/%s result=%s",
                        c.market_ticker, c.event_at.isoformat(timespec="minutes"),
                        c.implied_prob_close, c.yes_bid_close, c.yes_ask_close, c.result)
        logger.info("dry-run: not writing to S3")
        client.close()
        return 0

    files = write_candles_to_s3(candles)
    client.close()
    logger.info("done: wrote %d partition file(s)", files)
    return files


def ingest_current_day(series: str = SERIES_BTC_15M) -> dict[str, int]:
    """Re-fetch the current UTC day's markets (all statuses) and overwrite the
    day's partition. Returns a small summary (for the Airflow task / XCom)."""
    load_dotenv()
    client = KalshiClient()
    now = datetime.now(UTC)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    markets = client.list_markets(
        series,
        min_close_ts=int(start_of_day.timestamp()),
        max_close_ts=int(end_of_day.timestamp()),
    )
    candles = _fetch_candles(client, markets, series)
    files = write_candles_to_s3(candles)
    client.close()
    summary = {"markets": len(markets), "candles": len(candles), "files": files}
    logger.info("ingest_current_day: %s", summary)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill settled Kalshi 15-min BTC candlesticks.")
    parser.add_argument("--days", type=int, default=1, help="how many days back to backfill")
    parser.add_argument("--series", default=SERIES_BTC_15M)
    parser.add_argument("--dry-run", action="store_true", help="fetch + normalize but don't write")
    args = parser.parse_args()
    backfill(args.days, args.series, dry_run=args.dry_run)
