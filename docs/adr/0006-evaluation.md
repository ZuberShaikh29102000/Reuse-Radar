# ADR 0006 — Extraction evaluation: gold set, scoring, CI gate

Status: accepted · Date: 2026-10-02

## Gold set (`reuse_radar/eval/gold_set.jsonl`)

There are 6 papers and 60 items, chosen to cover the situations the pipeline meets:

| Paper | Situation covered | Items |
|---|---|---|
| 1995886 (HH→bbγγ) | many HEPData tables, mostly published | 15 |
| 2745375 (ttW) | large real gaps; HEPData holds only likelihood files | 23 |
| 1781293 (tZq) | no HEPData record at all | 8 |
| 1790256 (Lund plane) | short letter without sections | 2 |
| 1792133 (EW ZZjj) | small record, part published | 6 |
| 1775750 (VBF HH→bbbb) | mostly published | 6 |

**Labelling rules:**
- **What counts.** One item per reusable data result, usually one per results figure or table,
  plus key numbers stated in the text. **Simulation-only plots are not items**, even when HEPData
  hosts them; this matches the extraction prompt's definition.
- **Labels come from the paper, not the extractor.** Items were built from the paper's own
  captions and paragraphs, so the gold set is not simply a copy of what the model found.
- **Acceptable types.** `accept_types` lists every defensible type (for example a migration
  matrix with efficiency corrections), so a reasonable label is not counted as wrong.
- **HEPData status.** `on_hepdata` is true or false from HEPData's table list, or `null` where
  genuinely ambiguous. Examples: a single number that a published curve may cover; a likelihood
  scan whose full likelihood is published but whose curve is not. Null items do not count toward
  gap accuracy.
- **Evidence.** `evidence` lists **every place the result is stated** (caption, referring
  paragraph, conclusion). A test checks that each quote appears verbatim in the paper's filtered
  text.

**Revision 2.** The first scoring matched captions only. It counted correct products quoted from
a paragraph ("This Letter presents a double-differential cross-section measurement…") as both a
false positive and a miss, which put precision at 74% and the Lund-plane letter at 0%. The
alternative locations were then collected from **all** paragraph candidates of the six papers,
not only from passages the model quoted. One result missed in the first pass (the κλ allowed
range) was added. Both changes are recorded in each record's `revision_note`.

**Limitation.** **The labels are a draft by an AI assistant and have not been reviewed by a
physicist.** The report says so on every run. Review is the most valuable next step for the gold
set. The curator Accept/Reject reviews (Phase 6 UI) also grow the evaluation data over time.

## Scoring (`reuse_radar/eval/scoring.py`)

- **Match by evidence location, not wording.** A prediction matches a gold item when its
  verbatim span overlaps one of the item's quotes: one contains the other (whitespace ignored), or
  they share at least 40 characters. Descriptions are free text, and matching on them would make
  the score depend on phrasing.
- **One-to-one.** The best overlaps claim items first. Further predictions of an already-matched
  item are **duplicates**: correct products, so they do not hurt precision, but reported so the
  duplicate rate stays visible.
- **Metrics:** precision, recall and F1, overall and per product type (gold type for recall,
  predicted type for precision); type accuracy among matches; gap detection (missing/no_record
  versus published, with "uncertain" counted separately); cost (tokens, and dollars at configurable
  prices, default $0 on the free tier); and p50/p95 latency of the live calls.

## First baseline (extract_v1, gold rev 2, 2026-10-02)

| Metric | Value |
|---|---|
| Precision | **91%** |
| Recall | **65%** |
| F1 | **76%** |
| Type accuracy | 100% |
| Gap detection | precision 100%, recall 93% |

Per type, F1 is cross-section 95%, likelihood 100%, upper limit 88%, and **other 63%**. Recall
for "other" is 49%.

**Finding.** The extractor reliably finds headline results (limits, cross-sections,
likelihoods) but **skips data-versus-prediction distributions and yield tables**, especially in
control regions. That is the main recall loss and the target for prompt v2, before the bulk run
over the remaining ~505 papers.

**Cost.** The 6 gold papers took 10 requests and about 46K tokens (about 7.7K per paper), $0 on
the free tier. **Latency.** These cache entries predate latency tracking, so p95 shows n/a until
the fixtures are re-recorded live.

## CI gate

`python -m reuse_radar.eval.run_eval` (replay mode) re-runs the **current** extraction code on
the gold papers. Every LLM request is answered from the committed fixture cache
(`eval/fixtures`, about 270 KB: the six filter outputs plus the cache entries used). There is no
network, no API keys, no cost, and the result is deterministic. It fails when:

- a metric drops below `thresholds.json`, which is set just below the baseline: precision 0.85,
  recall 0.60, F1 0.70; or
- a request misses the fixture cache because the prompt, schema, chunking or model changed. The
  error explains how to re-record with `--record` (which needs API keys) and commit the new
  numbers. **A prompt change must come with fresh eval numbers.**

Thresholds are only ever raised. Lowering one to make a change pass defeats the gate.

*Rejected:* running live LLM calls in CI. It would cost quota on every push, need secrets in
CI, and be non-deterministic: one paper gave 27 and later 15 products on identical settings.

*Rejected:* scoring stored pipeline outputs in CI. That tests old outputs, not the code under
review. Stored mode remains available locally (`--source stored`), and it adds gap-detection
accuracy from the reconcile outputs.

## Known gaps in the evaluation

- **Run-to-run variance** (the same paper giving 27, then 15 products) is not measured by replay,
  which is deterministic by design. Measuring it needs repeated live runs. A `--repeats` live mode
  would cost about 46K tokens per repeat.
- **Six papers is small.** Per-type numbers for rare types (cut-flow, covariance) rest on a
  handful of items.
