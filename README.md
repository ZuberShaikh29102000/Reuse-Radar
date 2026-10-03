# Reuse Radar

**Finds reusable data from particle-physics papers that never reached HEPData.**

High-energy-physics papers present results such as cross-sections, exclusion limits,
likelihoods, efficiency maps and covariance matrices. The numbers behind those results belong in
[HEPData](https://www.hepdata.net), the field's public data repository, so others can reuse
them. Many never get there, and once an analysis team disbands the numbers are effectively lost.
Nobody knows *which* papers are missing *which* products.

Reuse Radar detects those gaps automatically. It reads each paper, lists the data products the
paper declares, each with a **verbatim quote** as evidence, checks what HEPData actually holds,
and ranks the missing items for curators. The results are available as a web UI, a public REST
API and an MCP server for AI agents.

It is the detection layer that neither INSPIRE nor HEPData provides. Everything runs on free
tiers.

**Live:**
- Curator UI: <https://reuse-radar.work-zubershaikh.workers.dev>
- API docs: <https://reuse-radar-api.onrender.com/api/docs>

The API runs on Render's free plan, which sleeps when idle, so the first request after a pause
can take about 50 seconds.

## How it works

```
INSPIRE ──► harvest ──► filter ──► extract ──► reconcile ──► store ──► Postgres ──► API ──► UI / MCP
            (papers)    (LaTeX     (LLM, with   (HEPData via   (load)              (read-only,
                        sections)  verified     DataCite +                          no model calls)
                                   quotes)      embeddings)
```

| Stage | What it does | Key rule |
|---|---|---|
| harvest | ATLAS papers 2020–2025 from INSPIRE (624 papers) | rate-limited and cached; fails loudly rather than truncating |
| filter | downloads LaTeX from arXiv; keeps captions, data statements and results paragraphs | pure Python, about 7× fewer tokens |
| extract | an LLM lists declared products (Groq `gpt-oss-120b`, Gemini fallback) | **every claim must quote the paper verbatim, or it is dropped** |
| reconcile | finds the HEPData record and tables (via DataCite) and matches products to them | local embeddings plus deterministic rules; every score stored |
| store | loads everything into Postgres (pgvector) | idempotent; curator reviews are never deleted |

The API, UI and MCP server only read precomputed rows. No model is ever called while serving.

## Current status

- **Corpus:** 624 papers harvested; 530 have LaTeX source and are queued for extraction.
- **Processed so far:** 53 papers, with 423 declared products: 150 on HEPData, 87 missing from
  the paper's HEPData record, 94 from papers with no HEPData record, and 92 for review. The rest
  is limited by the free LLM quota, about 20 papers a day.
- **Quality**, measured on 6 hand-labelled papers (`reuse_radar/eval`):

  | Measure | Value |
  |---|---|
  | Precision | 0.91 |
  | Recall | 0.65 |
  | F1 | 0.76 |
  | Gap detection | precision 1.00, recall 0.93 |

  The main weakness: data-versus-prediction plots and yield tables are under-extracted. The
  labels are an AI draft and have not yet been reviewed by a physicist.

## Run it locally

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), Docker and Node 22.

```sh
cp .env.example .env            # fill in INSPIRE_CONTACT_EMAIL, GROQ_API_KEY, GEMINI_API_KEY, DJANGO_SECRET_KEY
uv sync --extra pipeline
docker compose up -d db         # Postgres + pgvector
uv run python manage.py migrate

uv run python -m reuse_radar.pipeline.run            # all stages (free-tier limits apply)
uv run python manage.py runserver                    # API: http://127.0.0.1:8000/api/docs
cd frontend && npm install && npm run dev            # UI:  http://localhost:5173
```

Other entry points:

```sh
uv run python -m reuse_radar.eval.run_eval           # quality gate (replayed, no API calls)
uv run python -m reuse_radar.mcp.server              # MCP server (stdio), see docs/mcp.md
docker compose --profile airflow up -d               # Airflow UI on :8080 running the same pipeline
```

Checks, which CI runs on every push:

```sh
uv run ruff check . && uv run mypy reuse_radar tests scripts manage.py && uv run pytest
cd frontend && npm run typecheck && npm run lint && npm test && npm run build
```

## Deploy (all free tiers)

Supabase or Neon (database), Render (API), Cloudflare Pages (UI), and GitHub Actions (CI and the
nightly pipeline). Telemetry is optional: Grafana Cloud for traces and metrics, Sentry for errors.
Step by step: [docs/deploy.md](docs/deploy.md).

## Repository map

| Path | Contents |
|---|---|
| `reuse_radar/clients/` | INSPIRE, arXiv and HEPData (DataCite) clients, with rate limits and caching |
| `reuse_radar/pipeline/` | harvest, filter, extract, reconcile, store, and the shared runner |
| `reuse_radar/llm/` | provider router, response cache, output schema, prompts |
| `reuse_radar/api/` | Django + DRF: models, read-only API, curator reviews |
| `reuse_radar/mcp/` | MCP server (`find_reuse_gaps`, `get_reuse_profile`) |
| `reuse_radar/eval/` | gold set, scoring, CI gate |
| `frontend/` | React + TypeScript curator UI |
| `dags/` | Airflow DAG: a thin wrapper over the pipeline |
| `docs/adr/` | decision records: every non-obvious choice, with the alternatives rejected |
| `SPEC.md` | the project specification |

## Design decisions worth knowing

- **HEPData is read through DataCite.** hepdata.net answers scripts with a Cloudflare browser
  check, which we do not try to bypass ([ADR 0004](docs/adr/0004-reconcile-and-store.md)).
- **arXiv source comes only from `export.arxiv.org`,** arXiv's host for programmatic access, at 1
  request per 3 s ([ADR 0002](docs/adr/0002-arxiv-source-and-filter.md)).
- **The evidence gate is strict.** A quote may differ from the source only in whitespace
  ([ADR 0003](docs/adr/0003-llm-extraction.md)).
- **The quality gate replays committed LLM answers,** so CI is deterministic and free. A prompt
  change must come with fresh numbers ([ADR 0006](docs/adr/0006-evaluation.md)).

## Data sources and credit

Paper metadata from [INSPIRE-HEP](https://inspirehep.net). Source files from
[arXiv](https://arxiv.org) (thank you to arXiv for use of its open access interoperability).
HEPData record metadata via [DataCite](https://datacite.org). Reuse Radar links back to every
source and redistributes no paper text beyond short evidence quotes.
