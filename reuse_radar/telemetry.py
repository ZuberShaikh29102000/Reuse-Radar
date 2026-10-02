"""Optional telemetry export: OpenTelemetry (traces + metrics) and Sentry (errors).

Everything is off unless configured through standard environment variables, so local runs and
tests export nothing:

- OTEL_EXPORTER_OTLP_ENDPOINT (+ OTEL_EXPORTER_OTLP_HEADERS for auth), e.g. Grafana Cloud's free
  OTLP gateway. The spans and metrics the pipeline already records (per-stage spans; cache hit
  ratio, provider fallbacks, schema and evidence-verification failures, tokens) are then exported.
- SENTRY_DSN: error reporting (Sentry free tier). No personal data is sent (send_default_pii off).

If a variable is set but the library behind it is not installed, this raises instead of quietly
exporting nothing (SPEC section 2, item 7).
"""

from __future__ import annotations

import os

from reuse_radar import __version__


class TelemetryConfigError(RuntimeError):
    pass


def configure_sentry(component: str) -> bool:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError as exc:  # pragma: no cover - sentry-sdk is a core dependency
        raise TelemetryConfigError("SENTRY_DSN is set but sentry-sdk is not installed") from exc
    sentry_sdk.init(
        dsn=dsn,
        release=f"reuse-radar@{__version__}",
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        send_default_pii=False,
        traces_sample_rate=0.0,  # traces go to OpenTelemetry, not Sentry
    )
    sentry_sdk.set_tag("component", component)
    return True


def configure_otel(service_name: str) -> bool:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return False
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        raise TelemetryConfigError(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the exporter is not installed: "
            "install the `pipeline` extra (uv sync --extra pipeline)"
        ) from exc
    resource = Resource.create({"service.name": service_name, "service.version": __version__})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=30_000)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    # Both providers flush on interpreter exit (shutdown_on_exit defaults to True).
    return True


def configure_telemetry(service_name: str) -> dict[str, bool]:
    return {"otel": configure_otel(service_name), "sentry": configure_sentry(service_name)}
