"""One entry point for the whole batch pipeline, shared by Airflow and GitHub Actions.

Each stage is a plain function taking no arguments (configuration comes from the environment
and config/corpus.toml), so orchestrators stay thin wrappers (SPEC section 4: no business logic
in DAG files).

Quota exhaustion in the extract stage is an expected, recoverable condition on free tiers: the
stage stops, finished papers are kept, and the later stages (reconcile, store) still run so the
database reflects everything extracted so far. The next run resumes from the caches.

    python -m reuse_radar.pipeline.run                 # all stages
    python -m reuse_radar.pipeline.run --stages extract reconcile store
"""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable
from pathlib import Path

from opentelemetry import trace

from reuse_radar.config import Settings
from reuse_radar.log import configure_logging
from reuse_radar.pipeline.harvest import CorpusConfig

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
CORPUS_FILE = Path(os.environ.get("REUSE_RADAR_CORPUS", "config/corpus.toml"))


def _corpus() -> CorpusConfig:
    return CorpusConfig.from_toml(CORPUS_FILE)


def harvest() -> None:
    from reuse_radar.clients.inspire import InspireClient
    from reuse_radar.pipeline.harvest import harvest as run

    settings = Settings.from_env()
    client = InspireClient(
        contact_email=settings.inspire_contact_email, cache_dir=settings.cache_dir
    )
    run(client, _corpus(), settings.data_dir)


def filter_sources() -> None:
    from reuse_radar.clients.arxiv import ArxivClient
    from reuse_radar.pipeline.filter import run_filter

    settings = Settings.from_env()
    client = ArxivClient(contact_email=settings.inspire_contact_email, cache_dir=settings.cache_dir)
    run_filter(client, _corpus(), settings.data_dir)


def extract() -> None:
    from reuse_radar.llm.router import AllProvidersExhaustedError, router_from_env
    from reuse_radar.pipeline.extract import run_extract

    settings = Settings.from_env()
    try:
        run_extract(router_from_env(settings.cache_dir), _corpus(), settings.data_dir)
    except AllProvidersExhaustedError as exc:
        # Expected on free tiers: keep going so reconcile/store publish what is done.
        logger.warning("extract stopped on LLM quota; continuing", extra={"detail": str(exc)})


def reconcile() -> None:
    from reuse_radar.clients.hepdata import HepDataClient
    from reuse_radar.pipeline.reconcile import FastEmbedder, run_reconcile

    settings = Settings.from_env()
    client = HepDataClient(
        contact_email=settings.inspire_contact_email, cache_dir=settings.cache_dir
    )
    run_reconcile(client, FastEmbedder(settings.cache_dir / "models"), _corpus(), settings.data_dir)


def store() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "reuse_radar.api.settings")
    import django

    django.setup()
    from reuse_radar.pipeline.store import store_corpus

    store_corpus(_corpus(), Settings.from_env().data_dir)


STAGES: dict[str, Callable[[], None]] = {
    "harvest": harvest,
    "filter": filter_sources,
    "extract": extract,
    "reconcile": reconcile,
    "store": store,
}


def run(stages: list[str]) -> None:
    unknown = [s for s in stages if s not in STAGES]
    if unknown:
        raise ValueError(f"unknown stage(s) {unknown}; choose from {list(STAGES)}")
    with tracer.start_as_current_span("pipeline"):
        for name in stages:
            logger.info("stage starting", extra={"stage": name})
            with tracer.start_as_current_span(f"stage.{name}"):
                STAGES[name]()
            logger.info("stage finished", extra={"stage": name})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Reuse Radar batch pipeline.")
    parser.add_argument("--stages", nargs="+", default=list(STAGES), choices=list(STAGES))
    args = parser.parse_args(argv)
    configure_logging()
    from reuse_radar.telemetry import configure_telemetry

    configure_telemetry("reuse-radar-pipeline")
    run(args.stages)


if __name__ == "__main__":
    main()
