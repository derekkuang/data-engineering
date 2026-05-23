# Development Log

A running journal of work on the crypto data-engineering pipeline — what I did, why, and what I learned each day, so I can refer back and explain decisions in interviews. **Newest entries at the top.**

---

## 2026-05-22 — Coinbase → S3 ingestion built; first real Parquet lands

**Did:**
- Built the `ingestion/` module, structured to map cleanly to the Airflow DAG tasks already sketched in the README:
  - `ingestion/coinbase.py` — API client. Paginates the 300-candle-max endpoint in 5-hour forward windows, normalizes Coinbase's `[time, low, high, open, close, volume]` LHOC rows into an `OhlcvBar` dataclass with UTC microsecond timestamps, dedupes on `event_at`, retries on 429/5xx with exponential backoff, sleeps ~0.15s between requests to stay under the public ~10 req/s ceiling.
  - `ingestion/storage.py` — S3 Parquet writer. Groups bars by `(asset_id, event_at.date())` and writes one file per partition to `s3://.../raw/coinbase_ohlcv/dt=YYYY-MM-DD/<asset>.parquet`. Uses an **explicit pyarrow schema** that's the literal contract with the Athena external-table DDL — `event_at` and `ingested_at` are `timestamp[us, tz=UTC]`, prices/volume are `double`. `dt` lives in the S3 key, not the file (so Athena's partition projection derives it from the path).
  - `ingestion/backfill.py` — CLI runner (`uv run python -m ingestion.backfill --products BTC-USD,ETH-USD --days 60`) with a `--dry-run` mode.
- Added deps: `pandas`, `pyarrow`, `boto3`. Added a focused mypy override (`ignore_missing_imports` for boto3/botocore/pyarrow/pandas) so strict mypy stays clean without dragging in the heavy `boto3-stubs`.
- **Validated end-to-end on real data.** Dry-run pulled 1438 bars for BTC-USD over 24h in 5 paginated requests, zero retries. A 1-day real run wrote two partition files to S3 (the 24h fetch correctly crossed UTC midnight into two `dt=` partitions, proving partitioning is by *event time*, not run time). Read the Parquet back via boto3 + pyarrow — schema matches the Athena DDL exactly.
- Kicked off the full 2-month BTC + ETH backfill — first real lakehouse data.

**Learned (the bug worth remembering):**
- **The boto3 env-var precedence footgun was real, and `.env.example` warned about it almost word-for-word.** First S3 write failed with `InvalidAccessKeyId` even though `aws sts get-caller-identity` worked. Cause: at some point `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` got added to `.env` with stale values; `load_dotenv()` injected them into the process env; boto3's credential chain prefers env vars over `~/.aws/credentials`; AWS CLI doesn't (it reads the shared file directly), so the two diverged. Fix: stripped the two lines from `.env`, kept a `.env.bak`, added `.env.bak` / `.env.backup` to `.gitignore`. The lesson is the meta-lesson: when a spec calls out a footgun, the spec is right — re-read it before debugging.

**Learned (smaller things):**
- Pinning Parquet to an **explicit pyarrow schema** instead of letting it be inferred is the right discipline for a lakehouse — it makes the file format the contract that the warehouse DDL has to match, not the other way around. Catches drift at write time.
- Coinbase's LHOC row order (time, low, high, open, close, volume) really is easy to get wrong; the constant index names (`_TIME, _LOW, _HIGH, _OPEN, _CLOSE, _VOLUME`) make it impossible to mis-index in code review.
- Idempotent backfill = "one file per (asset, day), overwrite on re-run." No state, no watermark table, no DELETE — just put_object. Simpler is genuinely better here.

**Next:** define the Glue external table + Athena workgroup over the real S3 partitions (`docs/setup/03-athena-s3.md` Phases 1–4), wire the IAM permissions to let `crypto-de-pipeline` query, write `scripts/healthcheck_athena.py`. After that, dbt-athena staging.

---

## 2026-05-22 — Warehouse pivot #2: Snowflake → Athena (all-AWS)

**Did:**
- Went to sign up for the Snowflake trial and hit a wall: the only signup on offer is now the **Cortex Code CLI** flow (`signup.snowflake.com/cortex-code`) — credit card required, $2 auth hold, and it **auto-converts to a $20/month subscription on ~June 21** unless cancelled. The old card-free 30-day / $400-credit trial that the whole 5/20 decision rested on is no longer available to me.
- Re-opened the warehouse decision rather than pay $20/mo for a portfolio warehouse. Re-evaluated the three coherent options:
  - **S3 + BigQuery (cross-cloud)** — rejected *again*; BigQuery can't cleanly query S3 (Omni is enterprise/region-limited), so it means egress + two IAM models — the exact mismatch I killed on 5/20.
  - **GCS + BigQuery (all-GCP)** — free-forever tier, best dbt fit, strong keyword, but abandons the S3 + IAM work and needs GCP re-setup. (The GCP project is soft-deleted, restorable until ~June 19 — so not from scratch.)
  - **S3 + Athena (all-AWS)** — **chosen.**
- **Decision: S3 + Athena.** Reasons: zero rework (reuses the existing bucket + `crypto-de-pipeline` IAM as-is), serverless pay-per-scan (~cents at this data volume), **no expiry clock** so the warehouse can stay live indefinitely for the Loom/dashboard, and it's the literal lakehouse pattern this architecture is built around — discipline #1 (Parquet-in-S3 as source of truth, queried in place) is *native* in Athena, not extra work. Phase-3 Spark stays all-AWS (Glue/EMR).
- Tradeoffs accepted: dbt *incremental* models are fiddlier on Athena → I'll use **Iceberg** table format for clean merge/incremental. Resume keyword is a notch below Snowflake/BigQuery, but "Athena/Glue lakehouse" reads as data-platform work, which matches the project's framing well.
- **Build order flipped.** With Snowflake I wanted the warehouse up first (to not waste the 30-day clock). Athena has no clock and just defines a table *over whatever Parquet is already in S3* — so the order is now **ingestion-first**: land Parquet in S3 → then point Glue/Athena at it.
- Synced docs: marked `02-snowflake-s3.md` SUPERSEDED, wrote `docs/setup/03-athena-s3.md`, updated the README stack/architecture/steps, swapped the Snowflake `.env` block for Athena vars.

**Learned:**
- Vendor "free trials" can disappear underneath a decision — Snowflake now funnels signups into the card-gated Cortex Code subscription; the bare `signup.snowflake.com` no-card trial is effectively gone.
- Athena is the most architecturally honest fit for a lakehouse: it queries Parquet in object storage in place, so "warehouse reads via external table, not COPY INTO" stops being a discipline I have to enforce and becomes the default.
- The cost model inverts too: Snowflake = time-boxed credits (warehouse-first to not waste them); Athena = pay-per-scan with no clock (data-first, warehouse is just a schema over files).

**Next:** build `ingestion/coinbase.py` — backfill BTC-USD + ETH-USD 1-min bars, write Parquet partitioned `dt=YYYY-MM-DD` under `s3://.../raw/coinbase_ohlcv/`, watermark-driven incremental. Then Glue/Athena tables over it, then dbt-athena staging.

---

## 2026-05-20 — GCP setup, warehouse pivot to Snowflake, docs

**Did:**
- Set up GCP/BigQuery end-to-end: project `crypto-de-portfolio` with billing, three datasets (`crypto_raw`/`staging`/`marts`), service account `crypto-de-sa` with least-privilege roles (`bigquery.jobUser` + `dataEditor`), a key file, and `scripts/healthcheck_bigquery.py` passing end-to-end.
- Stepped back and reviewed the storage/warehouse architecture. Realized landing in **S3 (AWS)** while querying in **BigQuery (GCP)** was a cross-cloud mismatch.
- Compared three coherent single-cloud options — **S3+Athena**, **GCS+BigQuery**, **S3+Snowflake** — across industry usage, downstream flexibility, cost, and resume value.
- **Decision: Snowflake on AWS, reading S3.** Reason: strongest resume keyword, canonical "Snowflake + dbt + Airflow + S3" stack, and it reuses the S3 + IAM work already done. Trade-off accepted: 30-day trial, not perma-free — fine because the portfolio lives in the repo + README + a Loom, not a live warehouse.
- Updated docs: marked the GCP runbook SUPERSEDED, wrote `docs/setup/02-snowflake-s3.md`, updated the README stack/architecture/steps. Started this devlog.
- **Tore down GCP** (deleted the service-account key + the project) now that the warehouse is settled — no orphaned credentials.

**Learned:**
- GCP IAM service accounts & roles, and how they map to AWS IAM concepts.
- Application Default Credentials (how the client auto-discovers a key via `GOOGLE_APPLICATION_CREDENTIALS`).
- The lakehouse idea: open Parquet in object storage decouples storage from compute, so the warehouse is a swappable component — and that's why storage and compute should live in the *same* cloud to avoid egress.
- Preview of Snowflake's storage-integration trust model (no keys stored in Snowflake; it assumes an AWS IAM role).

**Next:** sign up for the Snowflake trial (AWS, `us-east-1`), then build storage integration → external stage → healthcheck.

---

## 2026-05-12 — AWS landing zone

**Did:**
- Created IAM user `crypto-de-pipeline` with a least-privilege inline policy (single bucket; list/get/put/delete only).
- Created the S3 raw-landing bucket `derekkuang-crypto-de-raw-546712138633-us-east-1-an`.
- Ran `aws configure`; credentials live in `~/.aws/credentials`. Deliberately kept AWS keys *out* of `.env` to avoid the env-var-precedence footgun with boto3.
- Set up the uv project (Python 3.12); deps: `httpx`, `python-dotenv`, plus dev group (`ruff`, `mypy`, `pytest`).
- Wrote `scripts/healthcheck_coinbase.py` — verifies Coinbase API reachability, response schema, and data freshness; exit codes so it can wire into CI/Airflow.

**Learned:** AWS IAM least-privilege policies; why env vars override `~/.aws/credentials` in boto3.

---

## 2026-05-10 – 05-11 — Direction & scoping

**Did:**
- Chose the domain: crypto OHLCV + on-chain data (BTC-USD, ETH-USD, 1-min bars).
- Set the framing rule: this is a **data-platform** project, not a price predictor — the ML is a small demo.
- Committed a phased roadmap (Phase 1 OHLCV → Phase 2 scale to ~20 pairs → Phase 3 tick data + PySpark → Phase 4 fan-out) and the 10 architectural disciplines that keep Phase 3 viable from day 1.
