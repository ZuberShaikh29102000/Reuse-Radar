# ADR 0007 — Orchestration and telemetry

Status: accepted · Date: 2026-10-02

## Orchestration

- **One runner.** `reuse_radar/pipeline/run.py` exposes one zero-argument function per stage
  (`harvest`, `filter_sources`, `extract`, `reconcile`, `store`) and a CLI that runs them in
  order inside OpenTelemetry spans.
- **Production** is `.github/workflows/pipeline.yml`: a daily cron plus manual dispatch.
  - Secrets come from repository secrets.
  - HTTP and LLM caches plus stage outputs persist between runs through `actions/cache`. A new
    entry is saved per run and the newest restored, so the free-tier backlog advances every night
    and reruns cost nothing.
  - A concurrency group prevents overlapping runs.
  - The manual `stages` input is passed through an environment variable, never interpolated into
    the script.
- **Local development** uses `docker compose --profile airflow up`, which runs Airflow 2.10.5
  (`standalone`) with `dags/reuse_radar_daily.py`. Each task in that DAG is one call to a
  runner function.
- **Guard.** A test parses every DAG file and fails if a task does anything but call a stage, or
  if the DAG imports anything beyond Airflow and the runner (SPEC section 4: no business logic in
  DAG files).
- **Quota exhaustion.** When the LLM quota runs out during extract, the stage logs a warning
  instead of failing the run, so reconcile and store still publish the papers finished so far.
  Every other error still fails the run loudly.

*Rejected:* running Airflow in production. No free host keeps a scheduler running, and GitHub
Actions cron is free and already where CI lives.

## Telemetry

`reuse_radar/telemetry.py`:

| Export | Turned on by | Where it goes |
|---|---|---|
| OpenTelemetry traces and metrics | `OTEL_EXPORTER_OTLP_ENDPOINT` (and `OTEL_EXPORTER_OTLP_HEADERS` for auth) | e.g. Grafana Cloud's free OTLP gateway |
| Errors | `SENTRY_DSN` | Sentry free tier, from both the pipeline and the API, with `send_default_pii=False` |

The exported spans and metrics are those the code already records: per-stage spans, cache
lookups, provider fallbacks, schema failures, evidence-verification failures, tokens, and gap
statuses. If a variable is set but its library is missing, startup raises instead of silently
exporting nothing.

The OTel SDK and exporter are in the `pipeline` extra. The web service records no OTel data; it
reports errors to Sentry.

## Left out of docker-compose (SPEC section 6 listed them)

- **OpenSearch.** Nothing needs it. Search is Postgres full-text search plus pgvector similarity
  ([ADR 0005](0005-api.md)). Running it would cost memory and add another moving part for no
  feature.
- **Prometheus and Grafana containers.** Telemetry is exported over OTLP to Grafana Cloud (free),
  which is where production metrics live. A local Prometheus would need a separate metrics
  endpoint that nothing in production uses. Add them only if someone needs offline dashboards.

## Verified

On 2026-10-02 the Airflow container (`apache/airflow:2.10.5-python3.12`) installed the project,
listed `reuse_radar_daily` with no import errors and all five tasks, and ran
`airflow tasks test reuse_radar_daily store` to SUCCESS against the compose Postgres. The shared
runner also ran `reconcile` and `store` live from the CLI.

## Not verified here

- The nightly workflow has not run on GitHub yet: the repository has no remote. It needs the
  secrets listed at the top of the workflow file.
- No live OTLP export or Sentry event has been sent, because no accounts exist yet. Only the
  "off unless configured" paths and Sentry initialisation are tested.
