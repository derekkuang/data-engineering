"""Publish the opportunity mart from Athena to the dashboard's local snapshot.

The dashboard reads a local Parquet (so it deploys with no AWS creds); this refreshes that
file from the live dbt mart. Run it after the pipeline's `dbt build`, or on a schedule, to
keep the deployed radar current.

Run:  uv run python -m dashboard.publish_snapshot
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pyathena import connect

OUT = Path(__file__).parent / "data" / "opportunity_snapshot.parquet"
QUERY = "SELECT * FROM crypto_marts.fct_kalshi_opportunity"


def main() -> int:
    load_dotenv()
    conn = connect(
        work_group=os.environ.get("ATHENA_WORKGROUP", "crypto_wg"),
        s3_staging_dir=os.environ["ATHENA_S3_STAGING_DIR"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    df = pd.read_sql(QUERY, conn)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    days = sorted(df["snapshot_day"].astype(str).unique())
    print(f"published {len(df)} rows ({len(days)} snapshot day(s): {days[0]}..{days[-1]}) -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
