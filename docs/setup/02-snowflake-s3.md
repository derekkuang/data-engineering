# Snowflake + S3 Setup Spec

**Status:** not started (2026-05-20). This is the **active** warehouse setup; it supersedes [`01-gcp-bigquery.md`](01-gcp-bigquery.md).

**Decision:** warehouse = **Snowflake on AWS**, reading raw Parquet from the existing S3 landing zone. Chosen for resume-keyword strength (Snowflake is the most-requested warehouse in DE postings; "Snowflake + dbt + Airflow + S3" is the canonical modern data stack) and because it reuses the S3 + IAM work already done. Trade-off accepted: 30-day / ~$400-credit trial, not perma-free — fine because the portfolio lives in GitHub + README + a Loom walkthrough, not a perpetually-live warehouse.

This keeps the same disciplines as the AWS/GCP work: least privilege, no long-lived secrets in the repo, and an end-to-end healthcheck before we trust it.

---

## Architecture (this layer)

```
Coinbase API → Python ingestion → S3 (raw Parquet, dt=YYYY-MM-DD)   [unchanged]
                                        │
                  Snowflake STORAGE INTEGRATION  ← assumes AWS IAM role (read-only)
                                        │
                              External STAGE over s3://.../raw/
                                        │
                        dbt-snowflake: staging → intermediate → marts
```

**Two AWS identities, by design:**
- IAM **user** `crypto-de-pipeline` (exists) — used by boto3 to **write** raw Parquet to S3.
- IAM **role** `snowflake-s3-read` (new) — assumed by Snowflake to **read** S3. Read-only, least privilege. Keeps the write path and the warehouse-read path separated.

---

## Phase 0 — Snowflake signup (browser, manual — Derek does this)

1. <https://signup.snowflake.com> → Standard edition.
2. **Cloud provider: AWS. Region: US East (N. Virginia) `us-east-1`** — match the S3 bucket's region to avoid cross-region data transfer.
3. Set username/password; enable MFA.
4. Note the **account identifier** (and account URL, like `https://<orgname>-<account>.snowflakecomputing.com`) — needed for dbt + the connector.

## Phase 1 — Snowflake objects (run in a Snowsight worksheet, role SYSADMIN)

```sql
CREATE WAREHOUSE IF NOT EXISTS crypto_wh
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60          -- suspend after 60s idle to conserve trial credits
  AUTO_RESUME = TRUE;

CREATE DATABASE IF NOT EXISTS crypto_db;
CREATE SCHEMA IF NOT EXISTS crypto_db.raw;
CREATE SCHEMA IF NOT EXISTS crypto_db.staging;
CREATE SCHEMA IF NOT EXISTS crypto_db.marts;
```

## Phase 2 — S3 storage integration (the secure cross-service trust)

This is a two-sided handshake (no AWS keys ever stored in Snowflake). There's a deliberate chicken-and-egg: create the integration referencing a role ARN, then read back Snowflake's principal and finish the AWS trust policy.

**2a. In Snowflake (role ACCOUNTADMIN):**

```sql
CREATE STORAGE INTEGRATION s3_int
  TYPE = EXTERNAL_STAGE
  STORAGE_PROVIDER = 'S3'
  ENABLED = TRUE
  STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::546712138633:role/snowflake-s3-read'
  STORAGE_ALLOWED_LOCATIONS = ('s3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/');

DESC INTEGRATION s3_int;   -- copy STORAGE_AWS_IAM_USER_ARN and STORAGE_AWS_EXTERNAL_ID
```

**2b. In AWS (Claude can run via CLI once we have the two values above):**
Create role `snowflake-s3-read` with:
- **Trust policy:** allow `STORAGE_AWS_IAM_USER_ARN` to `sts:AssumeRole`, conditioned on `sts:ExternalId = STORAGE_AWS_EXTERNAL_ID`.
- **Permissions policy:** `s3:GetObject` + `s3:ListBucket` scoped to the bucket and the `raw/*` prefix only.

## Phase 3 — External stage + file format

```sql
CREATE FILE FORMAT IF NOT EXISTS crypto_db.raw.parquet_ff TYPE = PARQUET;

CREATE STAGE IF NOT EXISTS crypto_db.raw.s3_raw
  STORAGE_INTEGRATION = s3_int
  URL = 's3://derekkuang-crypto-de-raw-546712138633-us-east-1-an/raw/'
  FILE_FORMAT = crypto_db.raw.parquet_ff;

LIST @crypto_db.raw.s3_raw;   -- proves the integration + trust policy work
```

**Raw-load sub-decision (defer until staging is built):** external tables over the stage (keeps S3 the single source of truth, Phase-3 Spark-readable) vs `COPY INTO` native tables (faster queries). Same fork as before; doesn't block setup.

## Phase 4 — `.env` + dbt

- Replace the `GCP_*` / `GOOGLE_APPLICATION_CREDENTIALS` block in `.env` with Snowflake vars: `SNOWFLAKE_ACCOUNT`, `SNOWFLAKE_USER`, `SNOWFLAKE_PASSWORD` (or key-pair auth), `SNOWFLAKE_ROLE`, `SNOWFLAKE_WAREHOUSE=crypto_wh`, `SNOWFLAKE_DATABASE=crypto_db`.
- `uv add dbt-snowflake snowflake-connector-python`
- dbt `profiles.yml` pointed at the account/warehouse/database above.

## Phase 5 — Healthcheck (the contract)

`scripts/healthcheck_snowflake.py`, mirroring `healthcheck_coinbase.py`: connect via `snowflake-connector-python`, run `SELECT 1`, confirm `crypto_wh` + `crypto_db` + the three schemas exist, and `LIST @s3_raw` succeeds (proving the S3 integration). Exit 0/1. Setup isn't "done" until this passes.

---

## Decisions captured

| Decision | Choice | Why |
|---|---|---|
| Warehouse | Snowflake on AWS | Strongest resume keyword; canonical modern data stack; reuses S3 |
| Region | `us-east-1` | Match S3 bucket; avoid cross-region transfer |
| S3 access | Storage integration → IAM role `snowflake-s3-read` | No keys in Snowflake; read-only; separate from the ingestion write user |
| Compute | `crypto_wh` XSMALL, AUTO_SUSPEND=60 | Minimize trial-credit burn |
| DB layout | `crypto_db` schemas raw/staging/marts | Medallion pattern |

## Follow-ups

- Decide external-table vs `COPY INTO` for the raw→warehouse boundary (when building dbt staging).
- Tear down the retired GCP project — see the teardown section in `01-gcp-bigquery.md`.
- Trial expiry plan: capture Loom + screenshots before day 30; code persists in GitHub regardless.
