"""Structure tests for the crypto_price_ingest DAG.

These run via `astro dev pytest` (in-container, so Airflow is importable). The DAG
lazy-imports the `ingestion` package inside its tasks, so it parses cleanly here
without the ingestion deps present — only the structure is asserted.
"""

from airflow.models import DagBag


def _dagbag() -> DagBag:
    return DagBag(include_examples=False)


def test_dagbag_imports_without_errors():
    bag = _dagbag()
    assert bag.import_errors == {}, f"DAG import errors: {bag.import_errors}"


def test_crypto_price_ingest_is_registered():
    dag = _dagbag().get_dag("crypto_price_ingest")
    assert dag is not None, "crypto_price_ingest DAG not found"
    assert set(dag.task_ids) == {"ingest_product", "summarize_ingest"}


def test_summarize_runs_after_ingest():
    dag = _dagbag().get_dag("crypto_price_ingest")
    summarize = dag.get_task("summarize_ingest")
    upstream_ids = {t.task_id for t in summarize.upstream_list}
    assert "ingest_product" in upstream_ids, "summarize must depend on ingest_product"


def test_dag_is_tagged_for_phase1():
    dag = _dagbag().get_dag("crypto_price_ingest")
    assert {"crypto", "ingest"} <= set(dag.tags)
