# ADR 0004 — Reconciliation: HEPData via DataCite, matching rules, data model

Status: accepted · Date: 2026-10-01

## 1. Getting HEPData's tables: DataCite, not hepdata.net

**Context.** HEPData's documented JSON endpoint is `hepdata.net/record/ins<id>?format=json`. On
2026-10-01 every scripted request to it returned **HTTP 403 with `Cf-Mitigated: challenge`**.
That is a Cloudflare browser check. It happened with an honest User-Agent, and again with an
honest `Accept: application/json`. `hepdata.net/robots.txt` also disallows `/search`.

**Decision.** We do not try to defeat bot protection. Instead we read HEPData's own metadata from
**DataCite**, where HEPData registers a DOI for every record version and every table:

| DOI | DataCite type | What we use |
|---|---|---|
| `10.17182/hepdata.<n>.v<k>` | Collection | `IsSupplementTo https://inspirehep.net/literature/<id>` to find the record; `HasPart` to list its parts |
| `…/t<j>` | Dataset (a table) | title `"<name>" of "<paper>"`; `descriptions[0]` is the table description, usually the paper's caption |
| `…/r<j>` | Other (a resource) | attached files, e.g. **full likelihoods** (`resourceType = "HS3 file"`, HistFactory JSON) |

- **Pace.** DataCite's documented limit for clients identified by an email in the User-Agent is
  1000 requests / 5 min / IP. We pace to 1 request/s.
- **Cache.** Responses are cached for 7 days, since records gain versions.

The lookup **fails loudly** in these cases:
- the number of parts found differs from the record's `HasPart` list, after fetching any part the
  search missed directly by its DOI. DataCite's search index can omit a DOI that exists: on
  2026-10-03 `hepdata.103063.v1/t175` was findable and linked to its record but absent from the
  search. A part that the direct lookup also cannot find (404) is still fatal.
- an INSPIRE id maps to two HEPData records
- a table title has an unknown format
- a page comes back short

The first of these caught a real bug: the first version skipped `/r` resources, which are the
likelihood files.

**Cross-check with INSPIRE.** The harvest also requests `external_system_identifiers`. INSPIRE
lists `{"schema": "HEPDATA", "value": "ins<id>"}` when it links a HEPData record (verified live).
If INSPIRE links a record but DataCite returns none, reconcile marks the paper `lookup_error`
(`processing_status = reconcile_error`) instead of emitting "no_record" gaps that would be
confidently wrong. On the first 15 processed papers the two sources agreed on all 15: 13 with a
record and 2 without. Corpus-wide, INSPIRE links a HEPData record for 326 of 624 papers.

*Rejected:* imitating a browser or solving the challenge. That evades the site's protection, and
SPEC section 2, item 5 requires respecting upstream APIs.

*Rejected:* hepdata.net search pages, which robots.txt disallows.

**Human follow-up:** if HEPData offers API keys or an allow-list for research tools, using that
would add the actual table contents. We use only titles and descriptions today.

## 2. Embeddings

`sentence-transformers/all-MiniLM-L6-v2` (384 dimensions, matching `vector(384)` in SPEC section 5)
runs locally through **fastembed** (ONNX Runtime) instead of the `sentence-transformers`
library. It is the same model weights without a PyTorch install: about 50 MB of dependencies
instead of about 1 GB, which matters for CI and GitHub Actions cron.

*Measured against `BAAI/bge-small-en-v1.5`* on the three fixture papers:

- **BGE rated everything 0.62–0.92.** A wrong pairing (differential cross-section ↔ inclusive
  table) scored 0.76, close to the correct one at 0.88.
- **MiniLM separates clearly.** Correct matches scored 0.75–0.81, wrong ones 0.2–0.4.

MiniLM was chosen.

## 3. Matching rules

For each product × table/resource we compute three signals:

- **embedding** similarity
- **caption overlap**: the share of the smaller word set found in the other, after stripping
  LaTeX and HTML. HEPData descriptions usually copy the paper's caption, so this is the strongest
  single signal.
- a **type bonus** (+0.10) when the part's name or description fits the product type (likelihood
  ↔ HistFactory/HS3, covariance ↔ correlation matrix, …)

`score = 0.5·embedding + 0.5·overlap + bonus`. The status is then decided as follows:

| Status | Rule |
|---|---|
| published | overlap ≥ 0.60, or embedding ≥ 0.70, or score ≥ 0.60 |
| — structural types* | otherwise, if a type-matching part exists: published if its embedding ≥ 0.50, else **uncertain** |
| uncertain | score ≥ 0.42 |
| missing | the paper has a HEPData record, but nothing matched |
| no_record | the paper has no HEPData record at all |

\*Likelihood, covariance, correlation, cut-flow, efficiency and acceptance products are usually
declared in prose rather than in a caption, so caption overlap cannot find them. What matters is
whether the record holds an object of that kind. Before this rule, the 2403.02793 covariance
products were false gaps next to 66 "Correlation matrix" tables.

**Calibration (three hand-checked papers):**

| Paper | Readiness | Outcome |
|---|---|---|
| 2112.11876 | 94 | Limits, likelihood scan, yields and systematics all matched the right tables (Tables 14–18). The resolution parameter is correctly *missing*. |
| 2401.05299 | 17 | HEPData has 2 tables and 4 likelihood files. The 6 differential cross-sections, yields and efficiency corrections are **real gaps**. The 2-D likelihood scans are *uncertain*, because the full likelihoods are published. |
| 2403.02793 | 61 | Most differential cross-sections matched. Migration matrices and purity/efficiency plots are *missing*. |

Thresholds are deliberately not tuned to every example. The ttW inclusive cross-section scores
0.59, just under 0.60, and lands in *uncertain*, which is the curators' queue. Every signal and
score is stored on the gap, so a reviewer can see why it was classified that way. The review table
then provides the data to re-calibrate.

*Not done yet:* inferring the paper's figure numbers from the LaTeX to match HEPData tables named
"Figure 7a". Only about 1 paper in 4 names tables that way. Others use HEPData's own "Table N"
sequence or internal names like `dataMC_VR_onLM_nomct`. Worth revisiting once the review data
shows how many matches it would fix.

**Near-duplicate products** (same type, cosine ≥ 0.90) are merged within a paper, keeping the
most confident one. The threshold is conservative: a lower one starts merging distinct results,
such as resonant and nonresonant limits.

**Severity** runs from 0 to 3:

- 3 for reinterpretation material: likelihoods, covariance and correlation matrices, efficiency
  maps, acceptance tables, cut-flows
- 2 for limits and cross-sections
- 1 for other results
- one level lower when the status is *uncertain*, and one level lower again when confidence is
  below 0.5, but never below 1 for a gap
- 0 for *published*

**Readiness score** = the type-weighted share of declared products found on HEPData. *Uncertain*
earns half credit.

## 4. Data model beyond the SPEC sketch

Additions to SPEC section 5, each for a concrete need:

- **`Paper.processing_status`** makes the 92 papers without an arXiv ID, and any failures,
  visible in the UI instead of silently absent.
- **`Paper.hepdata_record_doi` and `hepdata_version`** support linking and version-change
  detection.
- **`DeclaredProduct.fingerprint` and `is_current`.** Products are matched across reruns by
  `sha256(type, span, description)`, so reloads update rows in place. A product that a newer
  extraction drops is deleted, *unless reviewed*, in which case it is kept with `is_current=False`.
  `Review.declared_product` uses `on_delete=PROTECT`, so the database itself refuses to delete
  gold-set data.
- **`PublishedTable.kind` and `resource_type`** distinguish tables from resources such as
  likelihood files.
- **`Gap.match_score`, `embedding_similarity`, `caption_overlap` and `reconcile_version`** make
  every decision explainable and re-runnable.

**Indexes, for the triage query** (open gaps by severity, filtered by type and year), checked
with `EXPLAIN`:

| Index | Serves |
|---|---|
| partial `gap_triage_idx (-severity, status) WHERE status <> 'published'` | the default queue, read in order with no sort; published rows (not gaps) never bloat it |
| `product_type_idx`, `paper_date_idx` | filters, through primary-key joins |
| `paper_readiness_idx` | "worst papers" views |
| HNSW `vector_cosine_ops` on both embedding columns | Phase 5 similarity search |

At the expected size (about 7K products) every triage query is sub-millisecond. Denormalising
type and year onto `gap` was rejected as premature.

## 5. Pipeline shape

`reconcile` writes JSON per paper (`data/reconcile/…`), and a separate **`store`** stage loads
everything into Postgres. That keeps each stage testable without a database and idempotent. The
database can be rebuilt from files at any time.
