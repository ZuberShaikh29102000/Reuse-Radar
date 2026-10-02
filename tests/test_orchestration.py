from __future__ import annotations

import ast
from pathlib import Path

import pytest

from reuse_radar.llm.router import AllProvidersExhaustedError
from reuse_radar.pipeline import run as pipeline
from reuse_radar.telemetry import configure_otel, configure_sentry

DAGS = Path(__file__).parent.parent / "dags"


def test_run_executes_stages_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        pipeline, "STAGES", {name: (lambda n=name: calls.append(n)) for name in pipeline.STAGES}
    )
    pipeline.run(["harvest", "filter", "extract", "reconcile", "store"])
    assert calls == ["harvest", "filter", "extract", "reconcile", "store"]


def test_unknown_stage_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        pipeline.run(["harvest", "deploy"])


def test_extract_quota_exhaustion_does_not_stop_later_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import reuse_radar.llm.router as router
    import reuse_radar.pipeline.extract as extract_module

    def exhausted(*args: object, **kwargs: object) -> None:
        raise AllProvidersExhaustedError("quota")

    monkeypatch.setattr(extract_module, "run_extract", exhausted)
    monkeypatch.setattr(router, "router_from_env", lambda cache_dir: object())
    monkeypatch.setenv("INSPIRE_CONTACT_EMAIL", "test@example.org")
    pipeline.extract()  # must not raise


def test_dag_files_contain_no_business_logic() -> None:
    """SPEC section 4: DAGs are thin wrappers. Each task body is a single call to a stage."""
    for path in DAGS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert imports <= {
            "__future__",
            "airflow.sdk",
            "airflow.decorators",
            "reuse_radar.pipeline",
        }, imports
        tasks = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(isinstance(d, ast.Name) and d.id == "task" for d in node.decorator_list)
        ]
        assert tasks, f"{path.name} defines no tasks"
        for task in tasks:
            assert len(task.body) == 1, f"{path.name}:{task.name} has more than one statement"
            call = task.body[0]
            assert isinstance(call, ast.Expr) and isinstance(call.value, ast.Call)
            func = call.value.func
            assert isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            assert func.value.id == "pipeline" and func.attr in {
                "harvest", "filter_sources", "extract", "reconcile", "store"
            }  # fmt: skip


def test_telemetry_is_off_unless_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert configure_otel("test") is False
    assert configure_sentry("test") is False


def test_sentry_initialises_without_sending_pii(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    try:
        assert configure_sentry("test") is True
        assert sentry_sdk.get_client().options["send_default_pii"] is False
    finally:
        sentry_sdk.get_client().close()
