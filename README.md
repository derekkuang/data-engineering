# data-engineering

**Project: Real Data Engineering Pipeline**

---

**Goal**

Build a production-grade ELT pipeline that ingests raw public data, transforms it using dbt, orchestrates it with Airflow, stores it in a cloud warehouse, and surfaces it in a simple dashboard. The finished product should be something you can walk an interviewer through end-to-end and explain every architectural decision.

---

**The Stack**

- **Ingestion:** Python scripts pulling from a public API or CSV source
- **Orchestration:** Apache Airflow (scheduling, DAG management, dependency handling)
- **Warehouse:** Snowflake (free trial, 30 days) or BigQuery (free tier, generous limits)
- **Transformation:** dbt Core (free, open source) — this is the centerpiece
- **Storage layer:** AWS S3 or Google Cloud Storage as a staging area before warehouse load
- **Visualization:** Streamlit or a simple Metabase/Looker Studio dashboard on top of the warehouse
- **Infrastructure:** Docker for running Airflow locally, GitHub Actions for CI running dbt tests on every push
- **Version control:** Git throughout, everything public on GitHub

---

**The Dataset**

Pick one that is real, messy, and large enough to be interesting. Three good options:

**Option A — NYC Taxi Trips** (recommended)
- Source: NYC TLC public dataset, available via API or direct CSV download
- Why: Well-known, large (millions of rows), has time-series structure, joins across multiple tables (trips, zones, weather), and interviewers recognize it as a legitimate data engineering dataset
- Interesting questions to answer: average trip duration by borough and hour, revenue trends by month, surge patterns, tip percentage by payment type

**Option B — GitHub Archive**
- Source: GH Archive (gharchive.org), public BigQuery dataset or downloadable JSON
- Why: Event-driven data, complex schema, interesting for ML downstream, signals technical curiosity
- Interesting questions: most active repos by language over time, PR merge time trends, contributor growth patterns

**Option C — NOAA Weather + Energy Data**
- Source: NOAA Climate Data Online API + EIA (US Energy Information Administration) API
- Why: Time-series heavy, directly relevant to the SB Energy and SES Open Orbits roles you applied to, shows domain awareness
- Interesting questions: temperature-driven energy demand forecasting, regional consumption patterns

**Recommendation: Option A.** It is the most universally recognized data engineering learning dataset and the easiest to explain to any interviewer regardless of their background.

---

**The Architecture**

```
Raw Data Source (NYC TLC API / CSV)
        ↓
Python Ingestion Script
        ↓
AWS S3 / GCS (raw landing zone)
        ↓
Snowflake / BigQuery (raw schema — exact copy of source)
        ↓
dbt (transformation layer)
    ├── Staging models (clean column names, cast types, basic dedup)
    ├── Intermediate models (joins, enrichment, business logic)
    └── Mart models (final analytics-ready tables: trip_facts, zone_summary, daily_revenue)
        ↓
Airflow DAG (orchestrates ingestion → load → dbt run → dbt test)
        ↓
Streamlit / Looker Studio Dashboard
```

---

**dbt — The Centerpiece**

This is what interviewers will actually ask about. dbt is not just SQL — it is a disciplined way of thinking about data transformation. You need to understand and be able to explain:

**Model layers (this is the key concept):**
- `staging/` — one model per source table, light cleaning only, no business logic. Example: `stg_taxi_trips.sql` casts pickup_datetime to timestamp, renames vendorid to vendor_id, filters out null trip distances
- `intermediate/` — joins and enrichment that are reused across marts. Example: `int_trips_with_zones.sql` joins trips to the zone lookup table
- `marts/` — final business-facing tables. Example: `fct_trips.sql` is your fact table, `dim_zones.sql` is your dimension table

**Tests:**
- Write schema tests for every model: `not_null`, `unique`, `accepted_values`, `relationships`
- Write at least one custom singular test (e.g., assert that no trip duration is negative)
- Run `dbt test` in your GitHub Actions CI so tests fail loudly

**Documentation:**
- Write `description:` fields for every model and every column in your `schema.yml` files
- Run `dbt docs generate` and `dbt docs serve` — take a screenshot for your README
- This is what "documented, maintainable pipelines" looks like in practice

**A sample dbt model to write:**
```sql
-- models/staging/stg_taxi_trips.sql
with source as (
    select * from {{ source('raw', 'taxi_trips') }}
),
renamed as (
    select
        vendorid as vendor_id,
        tpep_pickup_datetime::timestamp as pickup_at,
        tpep_dropoff_datetime::timestamp as dropoff_at,
        passenger_count,
        trip_distance,
        pulocationid as pickup_location_id,
        dolocationid as dropoff_location_id,
        fare_amount,
        tip_amount,
        total_amount,
        payment_type
    from source
    where trip_distance > 0
      and fare_amount > 0
      and pickup_at < dropoff_at
)
select * from renamed
```

---

**Airflow — The Orchestration Layer**

Run Airflow locally with Docker using the official Astronomer Astro CLI (free, easiest setup) or the official `docker-compose.yaml` from the Airflow docs.

Your DAG should have these tasks in order:
1. `ingest_raw_data` — Python operator that pulls data from source and uploads to S3/GCS
2. `load_to_warehouse` — uses Snowflake's COPY INTO or BigQuery's load job to move raw data from S3 into the raw schema
3. `dbt_run` — BashOperator running `dbt run --select staging+ ` (run staging first, then downstream)
4. `dbt_test` — BashOperator running `dbt test`
5. `notify_on_failure` — email or Slack alert if any task fails

Set the DAG to run daily on a schedule. Even if you are not running it in production, the schedule being set correctly signals you understand orchestration as a production concept.

**A sample DAG skeleton:**
```python
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta
from ingestion.extract import ingest_taxi_data

default_args = {
    'owner': 'derek',
    'retries': 2,
    'retry_delay': timedelta(minutes=5),
    'email_on_failure': True,
}

with DAG(
    dag_id='taxi_pipeline',
    default_args=default_args,
    schedule_interval='@daily',
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=['taxi', 'production'],
) as dag:

    ingest = PythonOperator(
        task_id='ingest_raw_data',
        python_callable=ingest_taxi_data,
    )

    load = BashOperator(
        task_id='load_to_warehouse',
        bash_command='python scripts/load_to_snowflake.py',
    )

    dbt_run = BashOperator(
        task_id='dbt_run',
        bash_command='cd /dbt && dbt run --profiles-dir .',
    )

    dbt_test = BashOperator(
        task_id='dbt_test',
        bash_command='cd /dbt && dbt test --profiles-dir .',
    )

    ingest >> load >> dbt_run >> dbt_test
```

---

**Getting Started — Step By Step**

**Week 1: Infrastructure and raw ingestion**
1. Set up a free Snowflake trial account or GCP project with BigQuery
2. Install Astronomer Astro CLI, initialize an Airflow project (`astro dev init`)
3. Write the Python ingestion script — download 3 months of NYC taxi data, upload to S3 or GCS
4. Write the warehouse load script — use Snowflake's Python connector or BigQuery's Python client to COPY the data in
5. Confirm raw data is in your warehouse and queryable

**Week 2: dbt transformation layer**
1. Install dbt Core with the Snowflake or BigQuery adapter (`pip install dbt-snowflake` or `pip install dbt-bigquery`)
2. Initialize a dbt project (`dbt init`)
3. Write staging models for trips and zones
4. Write intermediate join model
5. Write at least two mart models (fct_trips, dim_zones)
6. Write schema tests for every model
7. Run `dbt docs generate` and take the screenshot

**Week 3: Orchestration, CI, and polish**
1. Wire the Airflow DAG to run ingestion → load → dbt run → dbt test in sequence
2. Set up GitHub Actions to run `dbt test` on every push (use dbt's official GitHub Action)
3. Write a strong README with architecture diagram, setup instructions, and a section explaining your design decisions
4. Build a simple Streamlit dashboard querying your mart tables directly via the Snowflake connector
5. Record a 2-minute Loom walkthrough of the pipeline end-to-end — link it in the README

---

**What To Say In An Interview**

The questions you will get and what you should be able to answer:

*"Walk me through your dbt project structure."*
Explain staging → intermediate → marts, why you separate concerns, what goes in each layer.

*"Why did you choose Snowflake over Redshift?"*
Free trial, column-oriented storage well-suited for analytical queries, easy Python connector, separation of compute and storage.

*"How do you handle schema changes in the source data?"*
dbt's `source freshness` checks, contract tests in dbt 1.5+, version control on your schema.yml.

*"What happens if a dbt test fails?"*
The Airflow DAG stops at the dbt_test task, sends an alert, and the mart tables are not updated until the issue is resolved. You do not serve bad data downstream.

*"What would you do differently if this were true production?"*
Add incremental models instead of full refreshes, implement proper secrets management (not hardcoded credentials), add row count anomaly detection, add a data catalog.

---

**How To Frame It On Your Resume**

Once built, the bullet points should read:

- Built an end-to-end ELT pipeline ingesting 3M+ NYC taxi records from the TLC API into Snowflake via AWS S3; orchestrated daily runs using Airflow with automated retry and failure alerting
- Designed a dbt transformation layer with staging, intermediate, and mart models; wrote 40+ schema and custom tests enforced via GitHub Actions CI on every commit
- Deployed a Streamlit analytics dashboard querying mart tables directly, surfacing trip revenue trends, borough-level demand patterns, and payment behavior insights

Those three bullets answer every "dbt, Airflow, Snowflake" gap you had this entire session.
