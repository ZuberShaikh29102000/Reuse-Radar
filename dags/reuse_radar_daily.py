"""Airflow DAG for local development: a thin wrapper over reuse_radar.pipeline.run.

No business logic lives here (SPEC section 4). Each task calls one stage function; production
runs the same functions from GitHub Actions (.github/workflows/pipeline.yml).
"""

from __future__ import annotations

import pendulum

try:  # Airflow 3
    from airflow.sdk import dag, task
except ImportError:  # Airflow 2.x
    from airflow.decorators import dag, task

from reuse_radar.pipeline import run as pipeline


@dag(
    dag_id="reuse_radar_daily",
    schedule="0 3 * * *",
    start_date=pendulum.datetime(2026, 10, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    tags=["reuse-radar"],
)
def reuse_radar_daily() -> None:
    @task
    def harvest() -> None:
        pipeline.harvest()

    @task
    def filter_sources() -> None:
        pipeline.filter_sources()

    @task
    def extract() -> None:
        pipeline.extract()

    @task
    def reconcile() -> None:
        pipeline.reconcile()

    @task
    def store() -> None:
        pipeline.store()

    harvest() >> filter_sources() >> extract() >> reconcile() >> store()


reuse_radar_daily()
