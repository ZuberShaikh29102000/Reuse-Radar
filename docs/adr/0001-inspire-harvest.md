# ADR 0001 — INSPIRE harvest: slicing, rate limiting, caching, failure policy

Status: accepted · Date: 2026-10-01

## Context

Phase 1 pulls the corpus (`config/corpus.toml`) from the INSPIRE REST API. SPEC section 2
requires: at most 15 requests per 5 s with 429s counting toward the quota, ~1 req/s pacing, a
5 s minimum backoff on 429, an identifiable User-Agent, no author emails, and a hard failure
rather than truncation past the 10,000-record ceiling.

## Facts verified against the live API (2026-10-01)

- Search responses have `hits.total` (an exact integer, e.g. 24,887 for `t higgs`; not capped
  at 10,000), `hits.hits`, and `links.next`. The last page has no `links.next`.
- `size` above 1,000 returns HTTP 400. Asking for results past 10,000 returns HTTP 400
  ("Maximum number of 10000 results have been reached").
- Hits carry `metadata.control_number`, `titles[].title`, `arxiv_eprints[].value`,
  `collaborations[].value`, and `earliest_date` when those fields are requested via `fields`.

## Decisions

**Slice by `de <year>`, not `date <year>`.** For `collaboration ATLAS and tc published` in 2021,
`date 2021` returned 109 records with earliest dates in 2019, 2020 and 2021, while `de 2021`
returned 67, all with a 2021 earliest date. `de` matches `earliest_date`, the date the data model
stores. Slices are therefore disjoint. The harvester also asserts that each record's
`earliest_date` falls in its slice year.

*Rejected:* `date`. It overlaps between slices, so a paper would be harvested in several years.

**Rate limiter: sliding window plus a minimum interval.** A deque of send timestamps enforces 15
per 5 s, and a 1 s minimum gap gives the ~1 req/s pace. Every attempt goes through the limiter
before it is sent, so throttled (429) attempts use up window slots.

*Rejected:* a token bucket. It allows bursts up to bucket size, and a burst is exactly what the
spec warns against.

**429 backoff:** `max(5 s × 2^attempt, Retry-After)`. The first wait is therefore one full
window. 5xx and transport errors back off `2^attempt` seconds. Other 4xx responses are not
retried. After `max_retries` the client raises.

**Cache:** `sha256(url)` names a JSON file holding `{url, fetched_at, body}`. The stored URL is
checked on read, and writes are atomic. Only 200 responses are cached.

- **Search pages expire after 24 h** (`search_cache_ttl_s`). Without expiry, any machine that
  keeps its cache (a developer laptop, the Airflow container) would replay the first snapshot
  forever and never see newly published papers.
- **Within one search, the first live page makes every later page live too.** A result set is
  never stitched together from snapshots taken at different times. The total-count check
  remains as a backstop.
- **Other `get_json` callers** (single records, which rarely change) get no expiry unless they
  pass `max_age_s`.

*Rejected:* caching forever, the original Phase 1 behaviour. Reruns were free, but on a
persistent cache new papers silently never appeared, which breaks SPEC section 2, item 7.
*Rejected:* no cache for searches. A 24 h window still makes same-day reruns and test loops free.

**Fail loudly.** The client raises in four situations:

- `hits.total > 10,000`, before any record is yielded
- the yielded count ≠ `hits.total` once pagination ends
- `hits.total` changes between pages
- `links.next` points outside the API base URL

Missing required fields raise `UnexpectedResponseError` or `HarvestError` rather than defaulting.

**No emails.** No author fields are requested. Any key containing "email" is also stripped
recursively before a response is cached or returned.

## Corpus composition (decided 2026-10-01)

A full harvest of 2020–2025 returned **624 records**:

- **92 have no arXiv eprint.** Many look like conference proceedings or detector papers.
- **About 35 belong to sub-collaborations** (`ATLAS Muon`, `ATLAS ITk`/`ITK`, `ATLAS TDAQ`,
  `ATLAS HGTD`, …), because `collaboration ATLAS` is a broad match. Six are joint ATLAS+CMS papers.

**Decision: keep the query unchanged.**

- Detector and performance papers do publish HEPData tables (efficiencies, resolutions). They fall
  inside the mission, which is about reusable products of any kind.
- Joint ATLAS+CMS combinations are among the most reused results in the field. Matching the
  collaboration exactly would drop them.
- Papers without an arXiv eprint stay in the harvest output, since they are real papers in the
  corpus. Later stages that need LaTeX mark them as having no source and skip them, so they are
  counted and visible rather than silently missing.

*Rejected:* adding `and eprint arxiv` (or similar) to the query. It hides those papers instead of
reporting them. Narrowing remains a one-line edit in `config/corpus.toml` if needed.
