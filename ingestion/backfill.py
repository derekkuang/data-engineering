"""Backfill OHLCV history from Coinbase into the S3 raw zone.

Usage::

    uv run python -m ingestion.backfill --products BTC-USD,ETH-USD --days 60
    uv run python -m ingestion.backfill --products BTC-USD --days 1 --dry-run

``--dry-run`` fetches from Coinbase but writes nothing to S3 — useful for
checking counts and the API path before committing a long backfill.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

from ingestion.coinbase import fetch_bars
from ingestion.storage import write_bars_to_s3

logger = logging.getLogger("ingestion.backfill")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--products",
        default="BTC-USD,ETH-USD",
        help="comma-separated Coinbase product ids (default: BTC-USD,ETH-USD)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=60,
        help="how many days of history to pull, ending now (default: 60)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch from Coinbase but do not write to S3",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    args = parse_args(argv)

    products = [p.strip() for p in args.products.split(",") if p.strip()]
    if not products:
        logger.error("no products given")
        return 1

    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    logger.info(
        "backfill: products=%s window=%s..%s dry_run=%s",
        products,
        start.isoformat(),
        end.isoformat(),
        args.dry_run,
    )

    total_bars = 0
    total_files = 0
    for product in products:
        bars = fetch_bars(product, start, end)
        total_bars += len(bars)
        if args.dry_run:
            logger.info("dry-run: %d bars for %s (not written)", len(bars), product)
            continue
        total_files += write_bars_to_s3(bars)

    logger.info(
        "backfill done: %d bars fetched, %d partition file(s) written", total_bars, total_files
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
