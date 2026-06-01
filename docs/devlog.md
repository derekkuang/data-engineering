# Development Log

A running journal of work on the crypto data-engineering pipeline — what I did, why, and what I learned each day, so I can refer back and explain decisions in interviews. **Newest entries at the top.**

---

## 2026-06-01 — Kalshi 15-min BTC ingestion: public data, backfill + live DAG (both green)

**Big pivot (made the unit far simpler):** verified via docs that **Kalshi market-data endpoints — markets + candlesticks — are PUBLIC and unauthenticated** on prod (`https://external-api.kalshi.com/trade-api/v2`). We only need read-only price/implied-prob and never place orders, so **no API key / demo env / KYC is needed**. Also confirmed the demo env lacks real activity, so we use **prod market data, read-only** ("monitor price/action, no trades"). The RSA-PSS signer is still built in `ingestion/kalshi.py` but optional (public mode by default).

**Instrument discovered:** series **`KXBTC15M`** = market **"BTC price up in next 15 mins?"** (e.g. `KXBTC15M-26JUN011745-45`). Each settled market = one 15-min window; its 1-min candlesticks give everything the goal needs at once:
- `price.*_dollars` (0..1) = **implied probability** → benchmark + feature
- `yes_bid`/`yes_ask` = the **spread** = transaction cost (cost-aware PnL)
- market `result` (yes/no) = the up/down **label** (forward-looking → stays OUT of the PIT mart)

**Built + verified:**
- `ingestion/kalshi.py` — public/optional-auth client (retry+backoff, 0.5s pacing for the rate limit), `list_markets` (cursor pagination), `get_market_candlesticks`, `KalshiCandle` + `normalize_market_candles`. Public healthcheck `scripts/healthcheck_kalshi.py` green.
- `ingestion/kalshi_storage.py` — S3 Parquet writer, explicit schema = Glue DDL contract, one file per `dt` (window open date), idempotent overwrite. `ingestion/kalshi_backfill.py` — `backfill(days)` (settled history) + `ingest_current_day()` (re-fetch today, overwrite — the live contract).
- **Backfilled 7 days** → 8 partitions, ~11k candles in `s3://.../raw/kalshi_btc_15min/dt=.../`.
- **Live Airflow task** `ingest_kalshi_15m` added to `crypto_price_ingest` (parallel to the OHLCV mapped tasks). Added `cryptography` to `airflow/requirements.txt` + `KALSHI_API_BASE` to `airflow/.env`, rebuilt the image; **task ran green in-container**: `{'markets': 97, 'candles': 1432, 'files': 2}` written to S3.
- Glue external table DDL written to `docs/setup/05-kalshi-ingestion.md` with partition projection.

**Learned / gotchas:**
- The **Glue table is a USER action**: the least-priv `crypto-de-pipeline` user is read-only on `crypto_raw`, so `CREATE EXTERNAL TABLE crypto_raw.kalshi_btc_15min` fails with `AccessDenied: glue:CreateTable` — must be run with owner/admin (Athena console), same as `coinbase_ohlcv`. DDL is in `docs/setup/05`.
- Public API **rate-limits (429)** quickly → client paces 0.5s/call + exponential backoff; backfill of 7 days (~660 markets) ran clean in ~3.5 min.
- During setup the demo key got pasted into `KALSHI_PRIVATE_KEY_PATH` (the field wants a *file path*) and a fragment surfaced in a tool error. Since we dropped auth, `.env` was cleaned to public config — **the unused demo key can be deleted in Kalshi** for hygiene.

**▶ PICK UP HERE NEXT TIME:**
1. **USER:** run the Glue DDL in `docs/setup/05-kalshi-ingestion.md` (Athena console, owner perms), then the verify `SELECT` should work as the pipeline user.
2. **dbt layer:** `stg_kalshi_btc_15min` (view over the raw table) → join the implied-prob as a **PIT-safe feature** into `fct_features_pit` (only data with `event_at <= T`); define the **forward 15-min up/down label** from `result` in a SEPARATE model (NOT in the PIT store).
3. Then the **model + walk-forward backtest** (Kalshi-benchmarked, net of spread/fees) and the Streamlit dashboard.

**Context for a fresh chat:** branch `phase1/athena-pivot-and-ingestion`. Kalshi work this session: new `ingestion/kalshi*.py`, `scripts/healthcheck_kalshi.py`, `docs/setup/05-kalshi-ingestion.md`, live task in `airflow/dags/crypto_price_ingest.py`, `cryptography` dep; `.env`/`.env.example` switched to public Kalshi config (`airflow/.env` gitignored holds `KALSHI_API_BASE`). 7 days of `kalshi_btc_15min` Parquet in S3. Local Airflow + Colima may still be running (`astro dev stop` + `colima stop` to shut down).

---

## 2026-06-01 — Resume-gap session: GitHub Actions CI (OIDC) + first runnable Airflow DAG

**Why this session (deliberate detour from the Kalshi plan):** a critical project review found the data layer (dbt + PIT + Iceberg) genuinely strong but the resume-legible pieces empty — Airflow, ML, dashboard, CI were all `.gitkeep` stubs while the README's resume bullets claimed Airflow DAGs, "40+ tests", CI, and a dashboard that don't exist. Chose to close the two highest-ratio gaps (CI + a real Airflow DAG) and fix the false claims, before resuming Kalshi.

**Did — CI (GitHub Actions, OIDC, no stored keys):**
- `.github/workflows/ci.yml` runs `dbt build` (compile → run → schema/PIT **tests**) against Athena on every push/PR. Concurrency-serialized per ref (Iceberg `MERGE` isn't concurrency-safe on one table).
- **AWS auth via GitHub OIDC federation** into a new role `crypto-de-ci` — the right call for a *public* repo (no long-lived keys anywhere). Wrote the role trust policy (`docs/setup/iam/github-oidc-trust-policy.json`), a CI S3 policy mirroring the user's inline S3 CRUD since a role can't inherit it (`ci-s3-access-policy.json`), and a step-by-step runbook (`docs/setup/04-github-oidc-ci.md`). The role reuses the existing athena-query + dbt-glue-write managed policies — same "grow perms deliberately" story, now on a federated role.
- `dbt deps` + `dbt parse` green locally → the project CI builds is structurally sound. **CI verified GREEN on GitHub** after the OIDC setup: role assumed via OIDC (no stored keys), `dbt build` + schema/PIT tests passed against Athena. (The first run failed as expected — pushed before the role existed; re-run after creating `crypto-de-ci` went green.)

**Did — Airflow (Astro Runtime 3 / Airflow 3, RUNS):**
- Stood up Colima (CLI-only Docker, no Docker Desktop) + Astro CLI; `astro dev init` in `airflow/`.
- `dags/crypto_price_ingest.py` — TaskFlow + **dynamic task mapping** over the product list (`ingest_product.expand(...)` → `summarize_ingest`). Ingestion imports are **lazy (inside the task)** so the DAG parses without the ingestion deps. Structure test in `tests/dags/`.
- **Ran it green end-to-end:** both mapped tasks (BTC-USD, ETH-USD) + summarize = success; **landed the `dt=2026-06-01` partition to real S3 — 1131 + 1131 = 2262 rows.** Verified the two Parquet files in the bucket + the task log line `crypto_price_ingest OK: 2262 rows`.
- **Caught a real trap:** `storage.py` writes one file per `(asset, day)`, overwrite. A naive "fetch last 15 min and write" would clobber the day partition down to 15 min. So the DAG **re-fetches the current UTC day (00:00→now) and overwrites** — idempotent, no data loss, matches the backfill contract.

**Learned (container wiring — the fiddly part):**
- The `ingestion/` package lives at the repo root, **outside the Astro Docker build context**, so it can't be `COPY`'d. Solution: **bind-mount `../ingestion`** into the scheduler (where LocalExecutor runs tasks) at `/usr/local/airflow/vendor/ingestion`, with `PYTHONPATH=/usr/local/airflow/vendor`.
- **Two different env mechanisms, easy to conflate:** (1) Astro injects the gitignored `airflow/.env` *into containers* (so PYTHONPATH + AWS creds reach the scheduler — env-var creds are the correct mechanism *inside* a container, distinct from the host where keys stay OUT of `.env`). (2) docker-compose `${VAR}` interpolation in the override file does **not** read `airflow/.env` → first start failed on `${INGESTION_SRC}` empty. Fix: a **relative** bind path (`../ingestion`), resolved against the project dir, no var needed.
- Colima gotcha: first `colima start` hung in VM provisioning even though the guest image had cached (under `~/Library/Caches/colima/caches/`, not `~/.lima`); `colima delete -f` + retry booted cleanly on the `vz` driver. Astro auto-appends `astro-run-dag` to requirements; Airflow 3 runtime is Python 3.13.

**Did — README honesty pass:**
- Added a **Project Status table** (built ✅ vs planned ⬜) as the top-of-README source of truth; rewrote the **resume bullets** to claim only what exists (killed the false Etherscan / "40+ tests" / two-DAGs / dashboard / walk-forward claims, split into "accurate today" vs "add once built"); flagged the stale on-chain + vol-nowcast sections and the BTC-directional/Kalshi pivot.

**▶ PICK UP HERE NEXT TIME:**
1. ✅ **DONE — AWS OIDC setup complete, CI green.** Created the OIDC provider, `crypto-de-ci` role (Web-identity trust scoped to `repo:derekkuang/data-engineering:*`), and 3 attached managed policies (`crypto-de-ci-s3`, `crypto-de-ci-athena`, `crypto-de-pipeline-dbt-glue-write` — note the Athena/Glue-read perms had to be re-created as a *managed* policy since the user's copy was inline and not role-attachable). Re-ran the workflow → green. NB: the repo is **private** (not public as first assumed) — OIDC still the right call.
2. **USER action (optional polish):** grab the Airflow UI screenshot for the README — stack runs at **http://airflow.localhost:6563** (Astro default login `admin`/`admin`); screenshot the green `crypto_price_ingest` grid. Shut down with `astro dev stop` + `colima stop` when done.
3. **Then resume the roadmap: Kalshi ingestion (Option B)** — the originally-planned next unit (RSA-PSS client, demo env, land `kalshi_btc_15min`, join implied-prob into the mart + define the forward label). See the prior two 2026-06-01 entries + `reference_kalshi_api.md`.

**Context for a fresh chat:** branch `phase1/athena-pivot-and-ingestion`. **This session's work is UNCOMMITTED** — new: `.github/workflows/ci.yml`, `docs/setup/iam/{github-oidc-trust-policy,ci-s3-access-policy}.json`, `docs/setup/04-github-oidc-ci.md`, the whole `airflow/` Astro project; modified: `README.md`. `airflow/.env` is gitignored (holds AWS keys for the container). Local Airflow stack + Colima may still be running.

---

## 2026-06-01 — CROWN JEWEL: fct_features_pit (Iceberg incremental) + PIT test, 12/12 green

**Did:**
- Built `models/marts/fct_features_pit.sql` — the point-in-time feature store, materialized as an **incremental Iceberg** table (`incremental_strategy='merge'`, `unique_key=['asset_id','event_at']`, `table_type='iceberg'`, `partitioned_by=['asset_id']`), with the `is_incremental()` watermark filter `event_at > (select max(event_at) from {{ this }})`. v1 is a thin assembler over `int_price_features` (Kalshi feature joins in later). Lands in Glue db `crypto_marts`.
- Added `macros/generate_schema_name.sql` — overrides dbt's default `<target>_<custom>` schema concat so `+schema: crypto_marts` lands in exactly `crypto_marts` (matches the IAM grant; default concat `crypto_staging_crypto_marts` would've been denied). `dbt_project.yml` marts block: `+materialized: incremental`, `+schema: crypto_marts`.
- **Demonstrated incremental live:** run 1 = `OK 180236` (full build, is_incremental false); run 2 immediately after = `OK 0` (watermark found nothing newer → merged 0 rows); row count held at 180,236 → merge idempotency proven (no dupes, grain test green). Cost scales with NEW data, not total.
- **Built the PIT singular test** `tests/assert_fct_features_pit_is_point_in_time.sql` — independently recomputes `rel_volume_20` for the 5 latest rows/asset using ONLY raw bars with `event_at <= T` (a backward-only join+rank), asserts equality with the stored feature. Returns 0 rows = pass.
- **Proved the test has TEETH** (it can fail): a 3-column inline (`stored` vs `backward_recompute` vs `forward_lookahead`) showed stored==backward exactly (3.905=3.905, 0.452=0.452…) while a forward-looking recompute diverged hard (3.905 vs 2.242, 0.452 vs 1.000) — i.e., if any window had peeked ahead, the test would surface rows and go red. Not a trivial green.
- Full suite: **`dbt test` = PASS 12/12** (staging/intermediate/mart grain+not-null + the PIT test).

**Learned (smaller):**
- dbt-athena writes a `view` with ZERO S3 data (Glue catalog object only) but an `incremental`/`iceberg` mart writes real Parquet+metadata to S3 — that's why the mart needed the Glue table/partition write IAM (already granted) and the views didn't.
- A passing test is only worth the proof that it CAN fail — always sanity-check teeth (the forward-vs-backward demo) before trusting a green PIT test.

**▶ PICK UP HERE NEXT TIME — Kalshi ingestion (Option B), now the next unit.** The price-only PIT store is done. Next: bring in the directional signal's other half.
1. New Python module `ingestion/kalshi.py` — RSA-PSS signed client (sign `timestamp+METHOD+path`, path sans query; headers KALSHI-ACCESS-{KEY,SIGNATURE,TIMESTAMP}), **develop against demo env `demo-api.kalshi.co`** first. Pull 15-min BTC up/down market **candlesticks** (1-min) → implied-prob history.
2. Land Parquet → `s3://.../raw/kalshi_btc_15min/dt=YYYY-MM-DD/`, mirror the storage.py contract pattern; Glue external table `crypto_raw.kalshi_btc_15min` (partition projection).
3. Then a `stg_kalshi_*` view + join the implied-prob feature into `fct_features_pit`; define the forward 15-min up/down **label** (separate, NOT in the PIT store).
4. See memory `reference_kalshi_api.md` for endpoints/auth and `project` memory for the trading/eval policy (edge-bet hold-to-expiry; metric = calibration + cost-aware PnL).

**Context for a fresh chat:** read this entry + the 3 memory files (project, collaboration, kalshi-api). All Phase-1 dbt work is COMMITTED on branch `phase1/athena-pivot-and-ingestion` (staging 9f88bfc, intermediate 35f7721, mart+PIT test cbd139a); working tree clean. To run dbt: `cd dbt && set -a && . ../.env && set +a && export DBT_PROFILES_DIR="$PWD"` then `uv run dbt run/test`. Next session starts on Kalshi ingestion (steps above).

---

## 2026-06-01 — Directional pivot (BTC 15-min + Kalshi) decided; int_price_features built

**Decided (the big one — a deliberate framing change):**
- **ML target is now BTC 15-minute up/down (directional)**, overriding the original "avoid directional, do volatility-nowcasting" framing rule. Reason it's defensible and not the naive-predictor trap: it's **anchored to Kalshi**, which runs liquid 15-min BTC up/down *binary* markets (~$70M/day) — so the market price IS an implied probability. Kalshi gives all three at once: the **benchmark** (beat the market-implied prior), a **tradable instrument** for a cost-aware backtest, and **exact 15-min horizon alignment**. The integrity guardrail is preserved, not dropped: walk-forward only, net of Kalshi spread/fees, benchmarked vs implied prob; honest bar = "beat the market after costs," not "profitable predictor." The label (sign of forward 15-min return) is forward-looking → stays OUT of `fct_features_pit`, joined only at train time.
- Verified Kalshi reality before committing (good instinct — it flipped two of my assumptions): 15-min BTC markets DO exist and are liquid; `GetMarketCandlesticks` (1/60/1440-min) makes historical backtest data fetchable; RSA-PSS auth; demo env at `demo-api.kalshi.co`. New raw source planned: `kalshi_btc_15min`. (Aside: CFTC approved Kalshi BTCPERP perp on 5/29 — brand new, ~no history, NOT the v1 instrument; the binary 15-min market is.) Captured in memory `reference_kalshi_api.md`.
- Scope calls: near-real-time **batch** via the planned 15-min Airflow DAG (live streaming deferred); Kalshi used as **benchmark + cost-aware trading backtest**.

**Did (modeling):**
- Built `models/intermediate/int_price_features.sql` (a **view**) — ~25 price/volume features per `(asset_id, event_at)` tuned for the directional target: multi-horizon log returns (1/5/15/60m), realized vol (rv 15/30/60m + short/long ratio), **range-based vol (Parkinson, Garman-Klass)** from the OHLC, ATR(14), **SMA-based RSI(14)** (chose SMA over recursive Wilder/EMA — negligible ML diff, SQL-clean), SMA-distance, Bollinger z-score, volume baselines (rel-volume, dollar-volume, signed-volume order-flow proxy), and PIT-safe calendar features (incl. sin/cos minute-of-day). Deferred rolling skew/kurtosis to v1.1 (tail/vol measures, little *directional* signal — start lean, add by feature importance).
- **The PIT rehearsal:** every rolling feature uses an EXPLICIT `rows between N preceding and current row` backward frame (never the default cumulative frame, never `following`), partitioned by `asset_id` so BTC windows never see ETH. This is the exact property the `fct_features_pit` crown-jewel test will later prove.
- Structured as layered CTEs (lags → per-bar building blocks → rolling aggregates → final ratios); used a named `WINDOW w` clause (Athena/Trino supports it). `dbt run`+`dbt test` green (PASS, grain unique + key not-nulls). Sanity-checked recent BTC rows: RSI in 0-100, tiny signed returns, positive vols. Note: feature cols are intentionally NOT `not_null`-tested — first ~60 bars/asset are warmup nulls by design.

**Learned (smaller):**
- `dbt show` appends its own LIMIT — pass `--limit N`, don't put `limit` in the inline SQL (double-LIMIT = parse error), and `--output` only takes `json`/`text`.
- Row-based frames assume contiguous minutes; BTC/ETH have ~0.7% missing bars, so "15 preceding rows" can span slightly >15 min. Accepted v1 simplification; documented the spine+forward-fill fix as future work.

**▶ PICK UP HERE NEXT TIME — decide next unit, then build:**
- **Option A (recommended): `fct_features_pit` mart** — Iceberg, incremental (`unique_key=['asset_id','event_at']`), built from `int_price_features` (price-only PIT store v1; Kalshi feature + label join in later). Then the **custom PIT singular test** (recompute a sample row from raw, assert equality) — the project's signature. This needs the dbt-glue-write policy's partition/Iceberg perms (already granted).
- **Option B: Kalshi ingestion** — new Python module (RSA-PSS auth, demo env first), land `kalshi_btc_15min` Parquet in S3, Glue external table, then join into the mart.
- Lean A first (delivers the crown jewel on data we already have), then B.

**Context for a fresh chat:** read this entry + the three memory files (project, collaboration, kalshi-api). dbt work on branch `phase1/athena-pivot-and-ingestion`; staging committed (9f88bfc), intermediate uncommitted.

---

## 2026-05-31 — dbt-athena stood up; first staging model (view) green end-to-end

**Did:**
- `uv add dbt-athena-community` (pulls dbt-core 1.11.11 + dbt-athena 1.10.1). First two `uv add` attempts no-op'd silently (flaky cache/network — "Resolved N packages", exit 0, but pyproject untouched); the third actually installed. Worth knowing: a 0 exit from `uv add` isn't proof the dep landed — verify pyproject/the venv.
- Scaffolded the dbt project under `dbt/`: `dbt_project.yml` (staging `+materialized: view` folder default), in-repo `profiles.yml` (env-var driven, **no secrets** — Athena auth rides the AWS credential chain; `database: awsdatacatalog` = catalog, `schema: crypto_staging` = Glue db dbt writes to), `packages.yml` (dbt_utils 1.3.3).
- **First model `stg_coinbase_ohlcv` as a VIEW** — decided view over incremental for staging: it's a thin rename/cast/dedupe with nothing expensive to amortize, always-fresh, zero stored copy, and Athena's column+partition pruning push *through* a view so it's near-free to re-run. Incremental/Iceberg is reserved for the marts where volume + compute justify the stateful machinery. Source declared in `_coinbase__sources.yml` (raw `crypto_raw.coinbase_ohlcv`); model renames open/high/low/close → `*_price`, drops impossible bars (null close, neg volume, high<low), dedupes to one row per `(asset_id, event_at)` via `row_number()`. Schema tests + dbt_utils grain-uniqueness test in `_stg_coinbase__models.yml`.
- **Verified:** `dbt run` green, `dbt test` PASS=5/5, and a `dbt show` count proves the view is exactly 1:1 with raw — **180,236 rows, 2 assets, 2026-03-24 → 05-26** (matches the 5/29 Athena healthcheck).

**Learned (the IAM iteration, as the spec predicted):**
- The 5/29 `athena-query` policy was deliberately **read-only on Glue**, so `dbt run` walked through a precise staircase of AccessDenied errors, each naming the next missing action: first `glue:CreateDatabase` (dbt auto-creates the model's schema db), then `glue:GetTableVersions` (dbt-athena's post-create version bookkeeping). Granted exactly those, scoped by resource ARN to **only `crypto_staging` + `crypto_marts`** — `crypto_raw` stays read-only to dbt. New artifact `docs/setup/iam/dbt-glue-write-policy.json`.
- **Inline-policy 2048-char wall.** Adding this as a *third inline* policy tripped IAM's "aggregate of all inline policies on a user ≤ 2048 non-whitespace chars" limit. Fix = make it a **customer-managed** policy (6144-char budget each, doesn't count against the inline aggregate, reusable, AWS-recommended). Editing a managed policy creates a new default version in place — no re-attach. Clean portfolio story: query-read and model-write are two separate, purpose-named policies, permissions grown exactly when a new capability needed them.
- A `dbt run` can mark a model ERROR on a *post-materialization* step (the `GetTableVersions` denial) while the view itself was already created — the tip-off was `dbt test` passing against the "failed" model. Errors aren't always all-or-nothing; read what step actually failed.

**▶ PICK UP HERE NEXT TIME — intermediate layer: `int_price_features.sql`.** Staging is green; next is feature computation per source. Concrete:
1. `models/intermediate/int_price_features.sql` over `{{ ref('stg_coinbase_ohlcv') }}` — per-`(asset_id, event_at)` features: 5/15/60-min returns, rolling realized volatility, RSI, Bollinger position. Compute with window functions **partitioned by `asset_id`, ordered by `event_at`** (never cross assets — discipline #9, no BTC-hardcoded logic). Likely `materialized: ephemeral` or `view`.
2. Watch for the **PIT trap** even here: every window must look *backward only* (`rows between N preceding and current row`), never `following` — that discipline is what the crown-jewel `fct_features_pit` test will later prove.
3. Then the mart `fct_features_pit` (Iceberg, incremental, `unique_key=['asset_id','event_at']`) + the custom recompute-from-raw singular test.

**Context for a fresh chat:** read this entry + the two memory files. dbt work is on branch `phase1/athena-pivot-and-ingestion`, not yet committed this session.

---

## 2026-05-29 — Athena warehouse stood up over the raw zone; healthcheck green

**Did:**
- Topped off the backfill (`--days 7`) before building the warehouse — overwrote the 5/22 partial-day partition and filled the 5/23–5/26 gap. Confirmed **idempotency-via-overwrite on real data**: re-running overlapping days rewrote identical files, no dupes, no watermark table needed.
- **Stood up the whole Athena layer** (`docs/setup/03-athena-s3.md`):
  - Athena **SQL** workgroup `crypto_wg` with a 1 GB per-query scan cutoff (cost guardrail) and results isolated in `athena-results/`, outside `raw/`.
  - Glue database `crypto_raw` + external table `coinbase_ohlcv` with **partition projection** on `dt` — no crawler, no `MSCK REPAIR`; Athena derives all 64 day-partitions from the S3 key pattern.
  - Authored the query IAM policy as a committed artifact (`docs/setup/iam/athena-query-policy.json`), least-privilege: Athena query lifecycle on `crypto_wg`, Glue **read-only** on `crypto_raw`, nothing more.
- Swapped the stale Snowflake block out of `.env` for the Athena vars; added `pyathena` (deferred `dbt-athena-community` to the dbt step).
- Wrote `scripts/healthcheck_athena.py`, mirroring the Coinbase one (staged OK/FAIL, 0/1 exit). It passes: **180,236 rows, 64 day-partitions, 2 assets, event_at 2026-03-24 → 05-26.**

**Learned (the bug worth remembering):**
- **Least-privilege bites in a precise, instructive way.** First healthcheck run failed at `SELECT 1` with `Unable to verify/create output bucket`. The ingestion policy granted object CRUD + `ListBucket`, so *writing* results worked — but Athena calls **`s3:GetBucketLocation`** (a *bucket-metadata* action, a different namespace from object actions) to verify the results bucket before every query, and that one action wasn't granted. Diagnosed precisely with two `aws s3api` probes (GetBucketLocation → AccessDenied; PutObject → OK), then added exactly that one action. Meta-lesson: object permissions and bucket-metadata permissions are separate in S3 IAM, and Athena needs both.
- Designing the healthcheck so **each stage exercises one IAM permission** (GetWorkGroup → glue:GetDatabase → StartQueryExecution → Glue table read) means a green run doubles as proof the policy is attached correctly — the healthcheck *found* the IAM gap instead of it surfacing as a runtime crash later.

**▶ PICK UP HERE NEXT TIME — stand up dbt-athena + first staging model.** The warehouse is live and healthchecked; the next sprint item is the modeling layer. Concrete steps:

1. `uv add dbt-athena-community` (pyathena is already in; dbt-core comes with the adapter).
2. Scaffold the dbt project: `dbt_project.yml` + `profiles.yml` (adapter `athena`, `work_group: crypto_wg`, `s3_staging_dir` = the `athena-results/` path, `schema`/staging db, `region_name: us-east-1`). Athena vars already live in `.env`.
3. `models/staging/_coinbase__sources.yml` — declare `crypto_raw.coinbase_ohlcv` as a dbt **source**.
4. `models/staging/stg_coinbase_ohlcv.sql` — thin **view**: rename/cast/standardize, 1:1 with source, no business logic. Add `not_null`/`unique`-style tests in the schema yml.
5. `dbt run` + `dbt test` — first model materialized and green.

This begins the medallion layer (staging → intermediate → marts) that leads to `fct_features_pit` (the PIT feature-store crown jewel) and its custom recompute-from-raw equality test. Decision to pause on when we get there: marts materialization = **Iceberg incremental** with `unique_key=['asset_id','event_at']` (the accepted Athena tradeoff). Reference: `docs/setup/03-athena-s3.md` Phase 5.

**Context for a fresh chat:** read this entry + `docs/setup/03-athena-s3.md` + the two memory files. Warehouse work from this session is committed on branch `phase1/athena-pivot-and-ingestion`.

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
