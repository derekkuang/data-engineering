# Athena + S3 Setup Spec

**Status:** ✅ stood up + healthchecked (2026-05-29) — workgroup, Glue DB, external table, and query IAM all live; `scripts/healthcheck_athena.py` passes (180,236 rows over 64 day-partitions). Next layer is dbt-athena staging. This is the **active** warehouse setup; it supersedes [`02-snowflake-s3.md`](02-snowflake-s3.md) (Snowflake's free trial disappeared — see that doc's banner) and [`01-gcp-bigquery.md`](01-gcp-bigquery.md).

**Decision:** warehouse = **Amazon Athena**, querying raw Parquet in the existing S3 landing zone **in place** via the Glue Data Catalog. Chosen after Snowflake's only remaining trial became a card-gated $20/month subscription. Athena wins on: zero rework (reuses the S3 bucket + `crypto-de-pipeline` IAM already built), serverless pay-per-scan (~cents at this data volume), **no expiry clock** (the warehouse can stay live for the Loom/dashboard indefinitely), and it *is* the lakehouse pattern this project is designed around — Parquet-in-S3 as the single source of truth, queried without a load step. Phase-3 PySpark stays all-AWS (Glue/EMR).

Same disciplines as before: least privilege, no long-lived secrets in the repo, and an end-to-end healthcheck before we trust it.

---

## Architecture (this layer)

```text
Coinbase API → Python ingestion → S3 (raw Parquet, dt=YYYY-MM-DD)   [the substance — build first]
                                        │
                        Glue Data Catalog: EXTERNAL TABLE over s3://.../raw/
                          (partition projection on dt — no crawler, no MSCK)
                                        │
                              Athena (serverless SQL, pay-per-scan)
                                        │
                        dbt-athena: staging → intermediate → marts
                          (marts as Iceberg tables → clean incremental MERGE)
```

**Why this is simpler than the Snowflake plan it replaces:** there is no storage-integration handshake, no second IAM role, no external stage, no `COPY INTO`. Athena reads the same S3 objects boto3 writes. The only new things are a Glue database, table definitions, an Athena workgroup, and a query-permissions policy on the existing IAM user.

**One IAM identity (for now):** the existing user `crypto-de-pipeline` writes Parquet (boto3) **and** runs Athena queries during local dev. It already has S3 access to the bucket; we add an Athena+Glue policy. (A separate read-only query identity is a possible later hardening, but for a single-developer portfolio it's over-engineering.)

---

## Build order (changed from the Snowflake plan)

Snowflake was warehouse-first to avoid wasting the 30-day clock. Athena has no clock and is just a schema over whatever Parquet exists in S3, so:

1. **Ingestion first** — land real BTC-USD + ETH-USD Parquet in `s3://.../raw/coinbase_ohlcv/dt=YYYY-MM-DD/`. (See `ingestion/coinbase.py`, next work item.)
2. **Then** Phases 1–4 below: workgroup → Glue DB → external table → healthcheck.
3. **Then** dbt-athena staging.

The Glue table DDL below assumes the column layout the ingestion writer produces, so it's specified here but *run* after ingestion exists.

---

## Phase 1 — Athena workgroup (cost guardrail)

Create a dedicated workgroup so query results land in a known place and we can cap accidental cost. In the Athena console (or CLI):

```sql
-- Athena console → Workgroups → Create workgroup: crypto_wg
--   Query result location: s3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/athena-results/
--   Enable "Override client-side settings"
--   Per-query data scan limit: 1 GB   (alarms/aborts a runaway scan — pure safety at this data size)
```

CLI equivalent (Claude can run this):

```bash
aws athena create-work-group --name crypto_wg --region us-east-1 \
  --configuration '{
    "ResultConfiguration": {"OutputLocation": "s3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/athena-results/"},
    "EnforceWorkGroupConfiguration": true,
    "BytesScannedCutoffPerQuery": 1073741824,
    "PublishCloudWatchMetricsEnabled": true
  }'
```

> Note: query results in `athena-results/` are **outside** the `raw/` prefix on purpose, so they never get picked up as source data by the Glue table.

## Phase 2 — Glue Data Catalog database (medallion)

```sql
CREATE DATABASE IF NOT EXISTS crypto_raw;
-- staging/marts databases are created by dbt-athena, not here.
```

## Phase 3 — External table over raw Parquet (partition projection)

Run **after** ingestion has written at least one `dt=` partition. Partition projection means Athena computes partitions from the `dt` range instead of needing a crawler or `MSCK REPAIR TABLE` — declarative, free, and self-maintaining as new days land.

```sql
CREATE EXTERNAL TABLE IF NOT EXISTS crypto_raw.coinbase_ohlcv (
    asset_id   string,
    event_at   timestamp,        -- microsecond precision (discipline #4)
    open       double,
    high       double,
    low        double,
    close      double,
    volume     double,
    ingested_at timestamp
)
PARTITIONED BY (dt string)
STORED AS PARQUET
LOCATION 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/coinbase_ohlcv/'
TBLPROPERTIES (
    'projection.enabled' = 'true',
    'projection.dt.type' = 'date',
    'projection.dt.format' = 'yyyy-MM-dd',
    'projection.dt.range' = '2024-01-01,NOW',
    'projection.dt.interval' = '1',
    'projection.dt.interval.unit' = 'DAYS',
    'storage.location.template' = 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/coinbase_ohlcv/dt=${dt}'
);

-- Proof the table + projection + S3 read all work:
SELECT dt, count(*) AS rows, min(event_at), max(event_at)
FROM crypto_raw.coinbase_ohlcv
GROUP BY dt ORDER BY dt;
```

> Keep `coinbase_ohlcv` and (later) `coinbase_trades` as **separate** raw tables — discipline #3; trades stays empty until Phase 3.

## Phase 4 — IAM: let `crypto-de-pipeline` query Athena

The user already has object CRUD + `ListBucket` on the S3 bucket from the original ingestion policy, which covers reading/writing the `athena-results/*` prefix. Missing pieces this policy adds: the Athena query lifecycle, read access to the Glue catalog under `crypto_raw`, and **`s3:GetBucketLocation`** — the one bucket-metadata action the ingestion policy lacked, which Athena calls to verify the results bucket before every query (the healthcheck surfaced this as `Unable to verify/create output bucket`; see devlog 2026-05-29).

Policy committed in the repo as **[`iam/athena-query-policy.json`](iam/athena-query-policy.json)**. It's deliberately *read-only on Glue* at this stage — dbt will need `Create*`/`Update*`/`Delete*` on Glue tables and partitions later, which we'll add as a second deliberate policy iteration (matches least-privilege properly, gives us a clean "grew permissions as needed" portfolio story).

**Apply (console, one-time, requires admin identity):**

1. IAM → Users → `crypto-de-pipeline` → Permissions → Add permissions → Create inline policy
2. JSON tab → paste the contents of [`iam/athena-query-policy.json`](iam/athena-query-policy.json)
3. Name it `crypto-de-pipeline-athena-query` → Create policy

After this is attached, every remaining step in this spec runs under `crypto-de-pipeline` via the local CLI.

## Phase 5 — `.env` + dbt

- Replace the `SNOWFLAKE_*` block in `.env` with Athena vars: `AWS_REGION`, `ATHENA_WORKGROUP=crypto_wg`, `ATHENA_S3_STAGING_DIR=s3://.../athena-results/`, `ATHENA_DATABASE=crypto_raw`. (No password — auth is the AWS credential chain via `~/.aws/credentials`, same as ingestion.)
- `uv add dbt-athena-community pyathena` (pyathena for the healthcheck + ad-hoc; dbt-athena-community is the maintained adapter).
- dbt `profiles.yml`: `type: athena`, `s3_staging_dir`, `region_name: us-east-1`, `work_group: crypto_wg`, `database: awsdatacatalog`, schemas for staging/marts. **Marts `table_type='iceberg'`** so incremental models use `MERGE` cleanly (the Athena tradeoff we accepted).

## Phase 6 — Healthcheck (the contract)

`scripts/healthcheck_athena.py`, mirroring `healthcheck_coinbase.py`: connect via `pyathena` (picks up AWS creds from the standard chain), run `SELECT 1`, confirm workgroup `crypto_wg` and database `crypto_raw` exist, and run `SELECT count(*) FROM crypto_raw.coinbase_ohlcv` (proves the table + S3 read + partition projection). Exit 0/1. Setup isn't "done" until this passes.

---

## Decisions captured

| Decision | Choice | Why |
| --- | --- | --- |
| Warehouse | Amazon Athena | Reuses S3+IAM, pay-per-scan ~$0 at this scale, no expiry clock, native lakehouse fit |
| Region | `us-east-1` | Same as S3 bucket; Athena must be in the data's region |
| S3 access | Existing `crypto-de-pipeline` IAM user + Athena/Glue policy | No new role/stage; Athena reads the same objects boto3 writes |
| Partitioning | Glue **partition projection** on `dt` | No crawler, no `MSCK REPAIR`, free, self-maintaining |
| Cost guard | Workgroup `crypto_wg`, 1 GB per-query scan cutoff | Caps any runaway scan; results isolated from `raw/` |
| dbt adapter | `dbt-athena-community`, marts as **Iceberg** | Iceberg gives clean incremental `MERGE` — the accepted Athena tradeoff |
| DB layout | Glue dbs `crypto_raw` / staging / marts | Medallion pattern |

## Follow-ups

- Confirm the raw table column order/types match what `ingestion/coinbase.py` actually writes (DDL above is the contract; reconcile when ingestion lands).
- External table vs. `CTAS`/Iceberg for staging: keep `raw` as external-over-S3 (source of truth, Spark-readable); let dbt materialize staging/marts as Iceberg.
- Trial-expiry plan is now moot — Athena has no clock. Still capture a Loom + screenshots for the portfolio narrative.
