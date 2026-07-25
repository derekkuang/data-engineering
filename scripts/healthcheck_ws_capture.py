"""Healthcheck / dead-man's-switch: is the in-play WS capture still landing data?

The review's blind spot: a silently-EMPTY capture window is indistinguishable from a healthy
idle window — both land nothing and the job stays green. So a broken capture (expired OIDC, a
rotated Kalshi secret, a WS-auth break) can rot for weeks while every run looks fine and the
toxicity marts quietly go stale.

This is the switch. It queries the raw ``crypto_raw.ws_features`` table for the most recent
snapshot that ACTUALLY LANDED and fails if nothing has landed within FRESHNESS_DAYS. Note we
count real rows via ``snapshot_at`` and never trust ``max(dt)``: ``dt`` is partition-PROJECTED,
so its max is the projected range end (≈today), present or not — it would hide the outage.

Wired as the last step of the daily pipeline: a stalled capture trips a red build + notification
instead of failing silent. A single idle cron is fine; only a multi-day gap alarms.

Exit 0 = fresh, 1 = stale / empty / unreachable. Usage:
    uv run python scripts/healthcheck_ws_capture.py
    FRESHNESS_DAYS=5 uv run python scripts/healthcheck_ws_capture.py
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

import pandas as pd
from pyathena import connect

FRESHNESS_DAYS = int(os.environ.get("FRESHNESS_DAYS", "3"))  # sports run ~daily; 3d gap = broken
LOOKBACK_DAYS = FRESHNESS_DAYS + 4  # partition-prune window, a bit wider than the threshold
TABLE = "crypto_raw.ws_features"

QUERY = f"""
select max(snapshot_at)                as latest,
       count(*)                        as rows_recent,
       count(distinct market_ticker)   as markets_recent
from {TABLE}
where dt >= date_format(current_date - interval '{LOOKBACK_DAYS}' day, '%Y-%m-%d')
"""


def main() -> int:
    print(f"Healthcheck: in-play WS capture freshness ({TABLE}, threshold {FRESHNESS_DAYS}d)")
    try:
        conn = connect(
            work_group=os.environ.get("ATHENA_WORKGROUP", "crypto_wg"),
            s3_staging_dir=os.environ["ATHENA_S3_STAGING_DIR"],
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
        df = pd.read_sql(QUERY, conn)  # noqa: S608 — no user input; constants only
    except Exception as exc:  # noqa: BLE001 — any failure to read = not healthy
        print(f"FAIL: could not query {TABLE}: {str(exc)[:200]}", file=sys.stderr)
        return 1

    latest = df["latest"].iloc[0]
    rows = int(df["rows_recent"].iloc[0] or 0)
    markets = int(df["markets_recent"].iloc[0] or 0)
    if pd.isna(latest) or rows == 0:
        print(f"FAIL: no capture rows landed in the last {LOOKBACK_DAYS} days — capture is DOWN. "
              "Check ws-capture.yml (OIDC role, KALSHI secrets, WS auth).", file=sys.stderr)
        return 1

    latest_ts = pd.Timestamp(latest).to_pydatetime()
    if latest_ts.tzinfo is None:
        latest_ts = latest_ts.replace(tzinfo=UTC)
    age = datetime.now(UTC) - latest_ts
    print(f"OK: latest snapshot {latest_ts.isoformat()} (age {age})")
    print(f"    {rows:,} rows / {markets} markets in the last {LOOKBACK_DAYS} days")

    if age > timedelta(days=FRESHNESS_DAYS):
        print(f"FAIL: newest capture is {age} old (> {FRESHNESS_DAYS}d) — capture has stalled.",
              file=sys.stderr)
        return 1
    print("Healthcheck PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
