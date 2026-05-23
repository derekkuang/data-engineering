"""Coinbase Exchange API client — fetch OHLCV candles as normalized bars.

The public candles endpoint returns at most 300 candles per request and orders
them newest-first. This module paginates forward over time windows, normalizes
Coinbase's ``[time, low, high, open, close, volume]`` rows into OHLCV bars with
UTC microsecond timestamps, and is rate-limit aware so a long backfill stays
under the public ~10 req/s ceiling.

Consumed by the backfill CLI (``ingestion/backfill.py``) and, later, the Airflow
``crypto_price_ingest`` DAG's ``fetch_new_bars`` task.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

COINBASE_BASE = "https://api.exchange.coinbase.com"
GRANULARITY_SECONDS = 60
MAX_CANDLES_PER_REQUEST = 300  # Coinbase hard limit; 300 * 60s = a 5-hour window
REQUEST_TIMEOUT_SECONDS = 30.0
RATE_LIMIT_SLEEP_SECONDS = 0.15  # ~6-7 req/s, comfortably under the public 10 req/s ceiling
MAX_RETRIES = 5

# Coinbase candle row layout is LHOC, not OHLC — easy to get wrong:
# [ time, low, high, open, close, volume ]
# https://docs.cdp.coinbase.com/exchange/reference/exchangerestapi_getproductcandles
_TIME, _LOW, _HIGH, _OPEN, _CLOSE, _VOLUME = range(6)


@dataclass(frozen=True)
class OhlcvBar:
    """One normalized minute bar. ``event_at`` is tz-aware UTC (microsecond precision)."""

    asset_id: str
    event_at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def _request_candles(
    client: httpx.Client, product: str, start: datetime, end: datetime
) -> list[list[float]]:
    """One candles request with retry/backoff on 429s and 5xx. Returns raw rows."""
    params: dict[str, str | int] = {
        "granularity": GRANULARITY_SECONDS,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    url = f"{COINBASE_BASE}/products/{product}/candles"

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 429 or resp.status_code >= 500:
                backoff = RATE_LIMIT_SLEEP_SECONDS * 2**attempt
                logger.warning(
                    "%s returned %s, backing off %.2fs (attempt %d/%d)",
                    product,
                    resp.status_code,
                    backoff,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            rows: list[list[float]] = resp.json()
            return rows
        except httpx.HTTPError as exc:
            last_error = exc
            backoff = RATE_LIMIT_SLEEP_SECONDS * 2**attempt
            logger.warning(
                "%s request error %r, backing off %.2fs (attempt %d/%d)",
                product,
                exc,
                backoff,
                attempt,
                MAX_RETRIES,
            )
            time.sleep(backoff)

    raise RuntimeError(
        f"Coinbase candles request for {product} failed after {MAX_RETRIES} attempts"
    ) from last_error


def fetch_bars(product: str, start: datetime, end: datetime) -> list[OhlcvBar]:
    """Fetch all 1-min bars for ``product`` in ``[start, end)``, ascending by time.

    Paginates forward in 300-candle windows. Deduplicates on ``event_at`` so the
    inclusive window boundaries Coinbase returns can't produce duplicate bars.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware (UTC)")

    window = timedelta(seconds=GRANULARITY_SECONDS * MAX_CANDLES_PER_REQUEST)
    bars_by_ts: dict[datetime, OhlcvBar] = {}
    request_count = 0

    with httpx.Client() as client:
        window_start = start
        while window_start < end:
            window_end = min(window_start + window, end)
            for row in _request_candles(client, product, window_start, window_end):
                event_at = datetime.fromtimestamp(row[_TIME], tz=UTC)
                bars_by_ts[event_at] = OhlcvBar(
                    asset_id=product,
                    event_at=event_at,
                    open=float(row[_OPEN]),
                    high=float(row[_HIGH]),
                    low=float(row[_LOW]),
                    close=float(row[_CLOSE]),
                    volume=float(row[_VOLUME]),
                )
            request_count += 1
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)
            window_start = window_end

    bars = [bars_by_ts[ts] for ts in sorted(bars_by_ts)]
    logger.info(
        "fetched %d bars for %s over %s..%s (%d requests)",
        len(bars),
        product,
        start.isoformat(),
        end.isoformat(),
        request_count,
    )
    return bars
