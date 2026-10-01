# Reuse Radar — project spec

Keep this file at the repo root. A coding agent should read it before every task.

---

## 1. Mission

Thousands of high-energy-physics papers describe reusable data products (cross-sections,
upper limits, efficiency maps, likelihoods, covariance matrices) that never reach HEPData,
the field's data repository. Once the analysis team disbands, that material is effectively
lost, and nobody currently knows *which* papers are missing *which* products.

Reuse Radar detects those gaps automatically and presents them as a ranked triage queue for
curators, a public API, and an MCP server for agents.

This is **not** a clone of INSPIRE or HEPData. It is the detection layer neither of them has.

---

## 2. Non-negotiable constraints

Violating any of these breaks the project. Do not "improve" past them.

1. **Zero running cost.** Free tiers only: Render (web), Supabase or Neon (Postgres),
   Groq and Google AI Studio (LLM), Cloudflare Pages (frontend), GitHub Actions (CI + cron),
   Grafana Cloud free (telemetry), Sentry free (errors).
2. **No LLM call in a request path.** All model work happens in the offline batch pipeline.
   The web service reads precomputed rows only.
3. **Every extracted claim carries a verbatim evidence span** copied from the source text.
   After extraction, verify the span appears literally in the source. If it does not, discard
   the claim. This is a hard correctness gate, not a nice-to-have.
4. **Cache every LLM response to disk**, keyed by sha256(provider + model + prompt).
   Reruns must cost zero API calls.
5. **Respect upstream APIs.** INSPIRE allows 15 requests per 5s per IP; 429s count toward the
   quota, so back off a minimum of one full 5s window. Run at ~1 req/s. Real User-Agent with a
   contact email. Never request or store author email fields.
6. **No secrets in the repo.** Everything via environment variables, `.env.example` committed,
   `.env` gitignored.
7. **Fail loudly, never silently truncate.** If a query exceeds INSPIRE's 10,000-record
   ceiling, raise. A corpus that is quietly wrong is worse than a job that crashes.

---

## 3. Scope

- Corpus: one collaboration, five years. Start with `collaboration ATLAS and tc published`,
  2020–2025. Roughly 500–1,500 papers.
- The pipeline must be corpus-agnostic: widening scope is a config change, never a code change.

Product types to detect:

```
cross_section | upper_limit | efficiency_map | likelihood | covariance_matrix
| acceptance_table | cutflow | correlation_matrix | other
```

---

## 4. Architecture

**Batch pipeline (offline):**

```
INSPIRE API ─┐
arXiv LaTeX  ├─> harvest ─> filter ─> extract ─> reconcile ─> score ─> Postgres
HEPData API ─┘
```

- `harvest` — INSPIRE REST API, sliced by year, `fields` param to trim payloads, follow
  `links.next`, disk cache.
- `filter` — pure Python. Pull only candidate sections from LaTeX (data availability
  statements, auxiliary material paragraphs, results sections, figure/table captions,
  appendix headers). Target a 10x token reduction. No model cost here.
- `extract` — LLM with strict JSON schema output. Pydantic validation, retry on parse
  failure, evidence-span verification, provider fallback (Groq -> Gemini) on 429.
- `reconcile` — fetch the HEPData record for the INSPIRE id, enumerate its tables, match
  declared products against published tables using local sentence-transformers embeddings
  plus deterministic rules.
- `score` — per-paper reuse readiness score plus a per-item gap list.

**Serving (live):** Postgres -> Django + DRF on Render -> React/TS UI, MCP server, public REST API.

**Orchestration:** Airflow in docker-compose for local dev; GitHub Actions cron calls the same
task functions in production. Task functions must be importable and runnable independently of
Airflow — no business logic inside DAG files.

---

## 5. Data model

```
paper(inspire_id PK, arxiv_id, title, collaboration, earliest_date,
      hepdata_record_id NULL, readiness_score, extraction_version,
      last_extracted_at)

declared_product(id PK, inspire_id FK, product_type, description,
                 evidence_span, evidence_section, confidence,
                 embedding vector(384))

published_table(id PK, hepdata_record_id, table_doi, name, description,
                embedding vector(384))

gap(id PK, declared_product_id FK, status, severity, matched_table_id NULL)

review(id PK, declared_product_id FK, verdict, reviewer, note, created_at)
```

`extraction_version` is required: changing the prompt must let you re-run and diff against
prior results. `review` rows are the growing gold set that feeds evaluation.

---

## 6. Repo layout

```
reuse_radar/
  clients/      inspire.py, arxiv.py, hepdata.py   (rate limits + caching live here)
  pipeline/     harvest.py, filter.py, extract.py, reconcile.py, score.py
  llm/          router.py, cache.py, schemas.py, prompts/
  api/          Django project: models, serializers, views, urls
  mcp/          MCP server exposing find_reuse_gaps, get_reuse_profile
  eval/         gold_set.jsonl, run_eval.py, report.py
dags/           Airflow DAGs — thin wrappers over pipeline functions only
frontend/       React + TypeScript
docker-compose.yml   Postgres, OpenSearch, Airflow, Prometheus, Grafana
docs/adr/       architecture decision records
```

---

## 7. Quality rules

- Type hints everywhere. `ruff` + `mypy` clean.
- `pytest` with recorded HTTP fixtures. Never hit a live API in a test.
- Every pipeline stage is independently testable and idempotent.
- Structured JSON logging with `inspire_id` on every line.
- OpenTelemetry spans per pipeline stage; custom metrics for schema failures,
  evidence-verification failures, cache hit ratio, provider fallbacks, tokens consumed.
- One ADR per non-obvious decision, explaining the alternatives rejected.
- Conventional commits. Real PR descriptions.

---

## 8. Build phases

Give the agent **one phase at a time**. Review and understand the output before moving on.

### Phase 1 — Harvest
> Implement `clients/inspire.py` per the constraints in SPEC.md section 2, item 5: a sliding-window
> rate limiter, disk cache keyed by URL hash, exponential backoff with a 5s minimum on 429,
> pagination via `links.next`, and a raise (not a truncation) when a query exceeds 10,000 results.
> Add `pipeline/harvest.py` that slices by year and writes JSONL. Tests use recorded fixtures.

### Phase 2 — Filter
> Implement `pipeline/filter.py`: given arXiv LaTeX source, extract only the sections likely to
> describe reusable data products. Return a list of (section_name, text) candidates. Pure Python,
> no model calls. Report the token reduction ratio as a metric. Include tests over three real
> papers' LaTeX committed as fixtures.

### Phase 3 — Extract
> Implement `llm/schemas.py` (Pydantic models for the extraction output), `llm/cache.py`,
> `llm/router.py` (Groq primary, Gemini fallback on 429, both via env-configured keys), and
> `pipeline/extract.py`. The extraction must return a list of declared products, each with
> product_type, description, and a verbatim evidence_span. Verify each span appears literally in
> the input text; drop any that does not, and increment an
> `evidence_span_verification_failures_total` counter. Retry once on schema-validation failure.

### Phase 4 — Reconcile and store
> Implement `clients/hepdata.py` and `pipeline/reconcile.py`. For each paper, look up its HEPData
> record by INSPIRE id, enumerate tables, and match declared products to published tables using
> sentence-transformers embeddings plus rules. Produce gap rows. Add Django models per SPEC.md
> section 5 with migrations and indexes chosen for the triage-queue query pattern.

### Phase 5 — Serve
> Build the DRF API: `/api/papers/{inspire_id}/gaps`, `/api/gaps` with filters for product_type,
> year, severity, and `/api/stats`. Add pgvector-backed search. No model calls anywhere in the
> request path. OpenAPI schema generated. Deploy config for Render.

### Phase 6 — UI, MCP, evaluation
> React + TypeScript triage queue with filters, an expandable evidence view, and accept/reject
> buttons writing to the `review` table. An MCP server exposing find_reuse_gaps and
> get_reuse_profile. An `eval/` harness scoring extraction against `gold_set.jsonl` and reporting
> precision/recall/F1 per product type, plus cost and p95 latency, wired into CI as a gate.

---

## 9. What the agent must not do

- Do not add a paid service, even a cheap one.
- Do not call an LLM from Django views, serializers, or any request-path code.
- Do not skip evidence-span verification to improve recall.
- Do not put business logic in Airflow DAG files.
- Do not invent INSPIRE or HEPData field names. If a field's existence is uncertain, write the
  code to fail loudly on absence and flag it in the PR description for a human to verify.
