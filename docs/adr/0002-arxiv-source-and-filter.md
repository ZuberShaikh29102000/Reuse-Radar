# ADR 0002 — arXiv source access and the LaTeX filter

Status: accepted · Date: 2026-10-01

## Part 1: fetching LaTeX from arXiv

### Context

Phase 2 needs each paper's LaTeX source. On 2026-10-01, `arxiv.org/robots.txt` **disallows
`/e-print` and `/src`** (with `Crawl-delay: 15`), and `export.arxiv.org/robots.txt` disallows `/`
for crawlers. arXiv's [bulk data page](https://info.arxiv.org/help/bulk_data.html) says
programmatic harvesting should use `export.arxiv.org`, which is "specifically set aside for
programmatic access", at a reasonable rate. It asks people not to download the *complete corpus*
programmatically and names Amazon S3 as the route for bulk access.

### Decision

- Fetch from **`export.arxiv.org/src/<id>` only**. That is the target `/e-print/<id>` redirects
  to. Requesting it directly avoids a second request that the rate limiter would not pace. In the
  first live run, before this change, every paper cost 2 requests.
- **One request every 3 s, one connection.** This is the figure in arXiv's API terms, stricter
  than the bulk page's "4 per second" bursts.
- **A real User-Agent** with a contact email.
- **A permanent cache** holding only the `.tex` files. A source tarball is about 5 MB, mostly
  figure PDFs; the `.tex` text is about 100–350 KB. Each paper is downloaded once, ever.
- **A 403 stops the whole stage** (`ArxivAccessDeniedError` is never caught per paper). arXiv
  says it treats continued requests after a 403 as an attack.
- **Payload handling:** a gzipped tar, a gzipped single file, or a PDF (no source; recorded as
  `no_latex`, not an error). Archive paths containing `..` or absolute paths are rejected.

The whole corpus is about 530 papers, a small targeted set rather than "the complete corpus", so
this is within the documented harvesting guidance.

*Rejected:* `arxiv.org/e-print`, which robots.txt explicitly disallows. *Rejected:* Amazon S3
bulk data, which is requester-pays and so breaks the zero-cost constraint, and is sized for the
whole archive. *Rejected:* the Kaggle dataset, which has metadata and PDFs, not LaTeX.

**Human follow-up:** arXiv asks that harvesting needs beyond these guidelines be discussed with
its administrators in advance. Widening the corpus to many collaborations (thousands of papers)
should trigger that conversation.

## Part 2: what the filter keeps

### Context

The SPEC targets a 10x token reduction. The filter's output is also the text that Phase 3's
evidence spans are checked against, so it must be **verbatim**: slices of the cleaned source,
never paraphrased.

### Decision

Clean the source first:

- expand `\input` and `\include`
- strip `%` comments, honouring `\%`
- remove `comment` environments
- drop comment-only lines, so paragraphs stay intact

Then keep:

1. **Captions.** Every figure and table caption, except Feynman diagrams and data-free captions
   in setup sections (introduction, detector, samples, reconstruction). Captions of results
   figures are the most direct statement of which products exist, and ATLAS typically puts a
   HEPData table behind each one.
2. **Data-statement sections, kept whole.** These are sections titled data availability,
   auxiliary or supplementary material, or HEPData.
3. **Paragraphs with availability language.** This means HEPData, Rivet, pyhf, SimpleAnalysis,
   "available at/from/on", "provided on/as", URLs, cut-flows, efficiency maps, covariance or
   correlation matrices, and published likelihoods.
4. **Paragraphs naming a product, in results-like sections or appendices.** Examples: upper
   limits, exclusion contours, fiducial or differential cross-sections, unfolded distributions,
   likelihood scans.
5. **Appendix headers.**

A letter with no section headers (only "Acknowledgements") is scanned in full as a results
section.

**Baseline for the ratio:** the document body, excluding the bibliography and the author list.
ATLAS author lists are about 250 KB, so counting them would inflate the ratio about 3x. Tokens are
estimated as `ceil(chars / 4)`.

### Measured results (FILTER_VERSION 1)

| Set | Result |
|---|---|
| The 3 committed fixtures | 5.8x, 7.0x, 6.5x |
| First 28 real corpus papers, median | 7.1x (range 1.7–14.9x) |
| Same 28 papers, aggregate | **5.6x**, about 3,150 candidate tokens per paper |

**The 10x target is not met, and that is deliberate.** Most of the kept text is captions of
results figures and tables. Those are the data products the system exists to find. Getting to
10x would mean dropping some of them, which trades recall for a number. The cost goal behind the
10x figure still holds: about 530 papers × 3.2k tokens ≈ 1.7M input tokens for the whole corpus.
That is spread across free-tier daily quotas, and cached afterwards.

The lowest ratios (1.7–3x) come from two kinds of paper. Short letters are almost entirely results.
Measurement papers have about 50 results figures (e.g. arXiv:2004.03969). Both cases are
legitimate.

### Rejected alternatives

- **Broad availability phrases** ("is provided", "are available"). They matched detector
  descriptions ("calorimetry is provided by…") and MC-sample text.
- **Keeping all captions.** About 40% of caption tokens were Feynman diagrams, sample-configuration
  tables, and BDT input-variable lists.
- **Dropping any caption containing "diagram" or "illustrates".** It lost an unfolded-data figure
  whose caption says "the inset triangle illustrates…" (arXiv:2004.03540). Regression test added.
- **Bare "signal efficiency" as a product term.** It is mostly setup prose.
- **Sending whole Results sections.** About 3x reduction, mostly float bodies and equations.
- **An ML or embedding pre-filter.** It adds model cost and dependencies to a stage the SPEC
  requires to be pure Python.

### Bugs found by running on real papers (not only fixtures)

- Letters without sections lost all their results paragraphs.
- The diagram rule dropped a data caption.
- The `/e-print/` redirect doubled request volume.

All three are fixed and covered by tests.
