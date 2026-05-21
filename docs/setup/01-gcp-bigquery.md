# GCP / BigQuery Setup Spec — ⚠️ SUPERSEDED

> **SUPERSEDED 2026-05-20.** The project switched its warehouse to **Snowflake on AWS** (strongest resume keyword; reuses the existing S3 landing zone). See [`02-snowflake-s3.md`](02-snowflake-s3.md) for the active setup. This file is kept for reference and as the record of the GCP IAM/least-privilege work (which transfers conceptually). The GCP resources below should be **torn down** — see the teardown section at the bottom.

**Status:** ✅ was completed end-to-end (SA, roles, key, healthcheck all verified) — now retired. Project ID: `crypto-de-portfolio`.
**Decision (2026-05-20):** Full GCP project with **billing enabled** (not sandbox). We rely on BigQuery's permanent free tier (10 GB storage, 1 TB queries/month) so real cost stays ~$0, while keeping no table-expiry and the ability to leave the project live as a portfolio demo.

This mirrors the discipline already used on the AWS side: a least-privilege service account, long-lived credentials kept out of the repo, and an end-to-end healthcheck before we trust it.

---

## Target end state

- A GCP project linked to a billing account, BigQuery API enabled.
- Three datasets (medallion pattern) in location `US`: `crypto_raw`, `crypto_staging`, `crypto_marts`.
- A least-privilege service account `crypto-de-sa` with a key JSON at `~/.config/gcp/crypto-de-sa.json`.
- `.env` populated with the real `GCP_PROJECT_ID`.
- A billing budget alert so a runaway query can't surprise us.
- `scripts/healthcheck_bigquery.py` authenticating via the SA, listing datasets, and running `SELECT 1`.

---

## Phase 0 — Console (browser, manual — Derek does this)

1. Sign in at <https://console.cloud.google.com> with your Google account.
2. **Billing:** create a billing account and add a card. (Optional: accept the $300 / 90-day free-trial credit — separate from, and on top of, the perma-free tier.)
3. **Create project:** name it `crypto-de-portfolio`. Note the actual **project ID** GCP assigns (it may append a number for global uniqueness) — we need it for `.env`.
4. **Link billing** to the new project.
5. Confirm the **BigQuery API** is enabled (on by default for new projects).

> Why a card if it's free? BigQuery's free tier doesn't require billing, but features we want (no 60-day table expiry, streaming inserts, leaving it live) do. The free-tier quotas still apply on top, so steady-state cost is $0.

## Phase 1 — gcloud CLI auth (local — Claude can run, or guide)

```bash
gcloud auth login                          # browser OAuth for your user account
gcloud config set project <PROJECT_ID>     # the ID from Phase 0 step 3
gcloud auth application-default login       # optional: ADC for local dev convenience
```

## Phase 2 — BigQuery datasets

```bash
bq --location=US mk -d <PROJECT_ID>:crypto_raw
bq --location=US mk -d <PROJECT_ID>:crypto_staging
bq --location=US mk -d <PROJECT_ID>:crypto_marts
```

`US` matches `BQ_LOCATION` in `.env`. Location is fixed at dataset creation — don't mix regions.

## Phase 3 — Service account (least privilege)

```bash
gcloud iam service-accounts create crypto-de-sa \
  --display-name="Crypto DE pipeline"

# Roles: jobUser lets it run load/query jobs; dataEditor lets it write tables.
# Granted at project level for now; tightening to per-dataset grants is a noted follow-up.
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:crypto-de-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"
gcloud projects add-iam-policy-binding <PROJECT_ID> \
  --member="serviceAccount:crypto-de-sa@<PROJECT_ID>.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataEditor"

mkdir -p ~/.config/gcp
gcloud iam service-accounts keys create ~/.config/gcp/crypto-de-sa.json \
  --iam-account="crypto-de-sa@<PROJECT_ID>.iam.gserviceaccount.com"
```

**Security:** that JSON is a long-lived credential. It lives in `~/.config/gcp/`, never in the repo. `.env` only points to its path via `GOOGLE_APPLICATION_CREDENTIALS` — same pattern as not putting AWS keys in `.env`.

## Phase 4 — Wire `.env`

- Set `GCP_PROJECT_ID=<PROJECT_ID>` (replace the `your-gcp-project-id` placeholder).
- `GOOGLE_APPLICATION_CREDENTIALS` already points to `~/.config/gcp/crypto-de-sa.json` — no change.

## Phase 5 — Cost guardrail

- In the console, create a **budget alert** (e.g. $5/month) on the billing account so an accidental large scan triggers an email.
- Optional: set a per-query `maximum_bytes_billed` cap in the BigQuery client to hard-fail expensive queries.

## Phase 6 — Verify (the contract)

Add the client lib and write a healthcheck that mirrors `scripts/healthcheck_coinbase.py`:

```bash
uv add google-cloud-bigquery
```

`scripts/healthcheck_bigquery.py` should: load `.env`, construct a client from the SA, list the three datasets, run `SELECT 1`, and print success. Setup isn't "done" until this passes — same bar as the AWS `sts get-caller-identity` check.

---

## Decisions captured

| Decision | Choice | Why |
|---|---|---|
| Sandbox vs billing | **Billing enabled** | No table expiry, streaming, live demo; free tier keeps cost ~$0 |
| Warehouse | BigQuery over Snowflake | Permanent free tier, no 30-day trial cliff |
| Datasets | `crypto_raw` / `crypto_staging` / `crypto_marts`, location `US` | Medallion pattern; matches `.env` |
| Auth | Service account key JSON, gitignored, `~/.config/gcp/` | Least privilege; mirrors AWS discipline |
| IAM scope | `jobUser` + `dataEditor` at project level | Simple start; per-dataset tightening is a follow-up |

## Teardown (do once Snowflake is verified)

This project is retired. Tear it down to avoid an orphaned credential and any billing exposure:

```bash
# 1) Delete the local service-account key (unused credential = liability)
rm -f ~/.config/gcp/crypto-de-sa.json

# 2) Delete the GCP project entirely (removes datasets, SA, and stops billing)
gcloud projects delete crypto-de-portfolio
```

Then revert the `GCP_*` / `GOOGLE_APPLICATION_CREDENTIALS` lines in `.env` (Snowflake config replaces them), and optionally `uv remove google-cloud-bigquery`.
