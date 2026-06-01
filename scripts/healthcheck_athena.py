"""Amazon Athena healthcheck.

Verifies the warehouse layer end to end: the Athena workgroup and Glue database
exist and are reachable under the current AWS credentials, a trivial query runs,
and the raw external table returns rows from S3 via partition projection. Exit 0
on success, 1 on any failure — so this can wire into CI or an Airflow sensor,
exactly like healthcheck_coinbase.py.

Each stage also exercises one permission from the crypto-de-pipeline-athena-query
IAM policy (GetWorkGroup, glue:GetDatabase, StartQueryExecution, Glue table read),
so a green run doubles as proof the policy is attached correctly.

Usage:
    uv run python scripts/healthcheck_athena.py
"""

import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv
from pyathena import connect
from pyathena.error import Error as PyAthenaError

RAW_TABLE = "coinbase_ohlcv"


def _require_env() -> tuple[str, str, str, str]:
    """Read the four Athena env vars; fail loudly if any is unset."""
    region = os.environ.get("AWS_REGION", "")
    workgroup = os.environ.get("ATHENA_WORKGROUP", "")
    database = os.environ.get("ATHENA_DATABASE", "")
    staging_dir = os.environ.get("ATHENA_S3_STAGING_DIR", "")

    missing = [
        name
        for name, val in [
            ("AWS_REGION", region),
            ("ATHENA_WORKGROUP", workgroup),
            ("ATHENA_DATABASE", database),
            ("ATHENA_S3_STAGING_DIR", staging_dir),
        ]
        if not val
    ]
    if missing:
        print(f"FAIL: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)
    return region, workgroup, database, staging_dir


def main() -> int:
    load_dotenv()
    region, workgroup, database, staging_dir = _require_env()
    print(f"Healthcheck: Athena workgroup={workgroup} database={database} region={region}")

    # 1. Workgroup exists and is enabled — exercises athena:GetWorkGroup.
    try:
        athena = boto3.client("athena", region_name=region)
        wg = athena.get_work_group(WorkGroup=workgroup)
        state = wg["WorkGroup"]["State"]
    except (ClientError, BotoCoreError) as e:
        print(f"FAIL: cannot read workgroup '{workgroup}': {e}", file=sys.stderr)
        return 1
    if state != "ENABLED":
        print(f"FAIL: workgroup '{workgroup}' is {state}, expected ENABLED", file=sys.stderr)
        return 1
    print(f"OK: workgroup '{workgroup}' reachable (state={state})")

    # 2. Glue database exists — exercises glue:GetDatabase.
    try:
        glue = boto3.client("glue", region_name=region)
        glue.get_database(Name=database)
    except (ClientError, BotoCoreError) as e:
        print(f"FAIL: cannot read Glue database '{database}': {e}", file=sys.stderr)
        return 1
    print(f"OK: Glue database '{database}' reachable")

    # 3. Trivial query — exercises StartQueryExecution + result staging end to end.
    try:
        conn = connect(
            s3_staging_dir=staging_dir,
            region_name=region,
            work_group=workgroup,
            schema_name=database,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        if cursor.fetchone() != (1,):
            print("FAIL: SELECT 1 did not return 1", file=sys.stderr)
            return 1
    except (PyAthenaError, ClientError, BotoCoreError) as e:
        print(f"FAIL: could not run SELECT 1: {e}", file=sys.stderr)
        return 1
    print("OK: SELECT 1 executed (query engine + result staging reachable)")

    # 4. The real proof: the external table returns rows from S3 via partition
    #    projection. count(distinct dt) confirms multiple day-partitions resolve.
    try:
        cursor.execute(
            "SELECT count(*), count(distinct dt), count(distinct asset_id), "
            f"min(event_at), max(event_at) FROM {database}.{RAW_TABLE}"
        )
        row = cursor.fetchone()
    except (PyAthenaError, ClientError, BotoCoreError) as e:
        print(f"FAIL: could not query {database}.{RAW_TABLE}: {e}", file=sys.stderr)
        return 1

    if row is None:
        print(f"FAIL: query against {RAW_TABLE} returned no result row", file=sys.stderr)
        return 1

    n_rows, n_days, n_assets, min_ts, max_ts = row
    if not n_rows:
        print(
            f"FAIL: {database}.{RAW_TABLE} has 0 rows — check the partition projection "
            "range covers the dt= partitions and that ingestion wrote Parquet",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {database}.{RAW_TABLE} returned {n_rows:,} rows")
    print(f"    {n_days} day-partition(s), {n_assets} asset(s)")
    print(f"    event_at range: {min_ts} .. {max_ts}")

    print("Healthcheck PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
