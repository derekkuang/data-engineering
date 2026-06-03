#!/bin/bash
# One KXBTC15M order-book capture for the current 15-min window. Invoked by launchd
# at each decision minute (:01/:16/:31/:46) — see the LaunchAgent
# com.derekkuang.kxbtc-orderbook. Captures the live executable book + BTC spot and
# appends rows to data/orderbook_snapshots.jsonl. Read-only / public API — no auth,
# no money. All output is logged to logs/orderbook_collector.log.
set -uo pipefail
cd /Users/derekkuang/data-engineering || exit 1
mkdir -p logs data
printf '=== %s ===\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> logs/orderbook_collector.log
/opt/homebrew/bin/uv run python -m ingestion.kalshi_orderbook \
  --snapshots 3 --interval 20 >> logs/orderbook_collector.log 2>&1
