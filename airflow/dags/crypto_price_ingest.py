"""crypto_price_ingest — land fresh Coinbase OHLCV + Kalshi 15-min implied-prob in S3.

Runs every 15 minutes. For each product it re-fetches the *current UTC day* from
00:00 to now and overwrites that day's partition file in S3.

Why re-fetch the whole day instead of only the last 15 minutes? The storage layer
(``ingestion/storage.py``) writes **one Parquet file per (asset, day), overwritten
on each run**. Writing only the last 15 minutes would clobber that file down to 15
minutes of data. Re-fetching the day-to-date and overwriting is idempotent, can't
destroy earlier same-day bars, and matches the backfill's "overwrite the day
partition" contract. Cost stays bounded (one day = at most 1,440 one-minute bars).

Task shape (Airflow 3 TaskFlow + dynamic task mapping):

    ingest_product["BTC-USD"]  ┐
    ingest_product["ETH-USD"]  ┼──► summarize_ingest
    ingest_kalshi_15m          ┘

``ingest_product`` is mapped over the product list, so the two assets ingest in
parallel and adding a third pair later is a one-line change. ``ingest_kalshi_15m``
re-fetches the current UTC day's KXBTC15M markets. Imports of the ``ingestion``
package are done *inside* each task so the DAG parses even where that package or
its deps aren't installed (e.g. the bare parse test).
"""

from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

# BTC only — the model targets BTC 15-min direction (Kalshi KXBTC15M). The task is
# still dynamically mapped over this list, so adding ETH back is a one-line change.
PRODUCTS = ["BTC-USD"]


@dag(
    dag_id="crypto_price_ingest",
    schedule="*/15 * * * *",
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": pendulum.duration(minutes=5)},
    tags=["crypto", "ingest", "phase1"],
    doc_md=__doc__,
)
def crypto_price_ingest():
    @task
    def ingest_product(product: str) -> dict:
        """Fetch the current UTC day-to-date for one product and overwrite its
        S3 partition. Returns a small, XCom-friendly summary."""
        from datetime import UTC, datetime

        from ingestion.coinbase import fetch_bars
        from ingestion.storage import write_bars_to_s3

        now = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)

        bars = fetch_bars(product, start_of_day, now)
        files_written = write_bars_to_s3(bars)
        return {
            "product": product,
            "rows": len(bars),
            "files_written": files_written,
            "day": start_of_day.date().isoformat(),
        }

    @task
    def ingest_kalshi_15m() -> dict:
        """Re-fetch the current UTC day's KXBTC15M markets (implied-prob path,
        bid/ask spread, settled results) and overwrite that day's S3 partition —
        same idempotent re-fetch-the-day contract as the Coinbase task."""
        from ingestion.kalshi_backfill import ingest_current_day

        return ingest_current_day()

    @task
    def summarize_ingest(results: list[dict], kalshi: dict) -> None:
        """Fail loudly if Coinbase landed zero rows; otherwise log the totals.

        Gating on row counts here is the seed of load-level data-quality checks
        (row-count anomaly detection is noted as future work in the README)."""
        import logging

        log = logging.getLogger("airflow.task")
        total_rows = sum(r["rows"] for r in results)
        for r in results:
            log.info("ingested %(rows)s rows for %(product)s -> dt=%(day)s", r)
        log.info("kalshi: %(markets)s markets, %(candles)s candles, %(files)s file(s)", kalshi)
        if total_rows == 0:
            raise ValueError("Coinbase ingest landed 0 rows — upstream API issue?")
        log.info(
            "crypto_price_ingest OK: %d Coinbase rows + %d Kalshi candles",
            total_rows, kalshi["candles"],
        )

    summarize_ingest(ingest_product.expand(product=PRODUCTS), ingest_kalshi_15m())


crypto_price_ingest()
