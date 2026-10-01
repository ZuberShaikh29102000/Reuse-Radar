# ADR 0003 — LLM extraction: providers, settings, evidence verification

Status: accepted · Date: 2026-10-01

## Context

Phase 3 turns each paper's filtered passages into a list of declared data products. The SPEC
requires:

- free tiers only (Groq first, Gemini as fallback)
- strict JSON output
- one retry on schema failure
- every LLM response cached on disk
- a hard gate: each claim's evidence span must appear in the source

The facts below were checked against the live APIs on 2026-10-01.

## Provider facts that shaped the design

| | Groq `openai/gpt-oss-120b` | Gemini `gemini-3.5-flash` |
|---|---|---|
| Free limits | 30 RPM, **8K tokens/min**, 1K req/day, **200K tokens/day** | per project, shown only in AI Studio |
| Structured output | `response_format.json_schema` with `strict: true` (constrained decoding) | `responseJsonSchema` + `responseMimeType` |
| Reasoning | `reasoning_effort` low/medium/high; counts as output tokens | `thinkingLevel`; counts against `maxOutputTokens` |

Other observations from the live APIs:

- `gemini-2.5-flash` returns 404 "no longer available to new users".
- `gemini-3.8-flash` returned 503 "high demand" on repeated attempts.
- Gemini 3 models reject `thinkingBudget` (use `thinkingLevel`).
- Groq strict mode **intermittently fails its own output** with HTTP 400 `json_validate_failed`.
  The same request succeeds on retry.

## Decisions

**Settings, chosen by measurement on arXiv:2112.11876, whose ~12 products were checked by hand:**

| Setting | Products found | Quotes verified | Likelihood scan? | Output tokens |
|---|---|---|---|---|
| Groq, `low` effort | 5 | 5 | missed | 0.7K |
| **Groq, `medium` effort** | **12** | **11** | found | 3.6K |
| Gemini, `low` thinking | 5 | 4 | found | 0.7K |
| Gemini, `medium`, 2.5K output cap | 3 | 3 | found | truncated mid-string |
| **Gemini, `medium`, 12K output cap** | **8** | **8** | found | 6.2K |

We use Groq at `medium` and Gemini 3.5 Flash at `medium` with a 12K output floor. `low` looks
cheaper, but it misses about half the products. A cheap extractor that misses products defeats the
purpose of a gap detector.

**Batching for Groq's 8K tokens/minute.** Passages are grouped into requests of ≤2,000 passage
tokens with a 3,000-token output cap. That gives about 6.4–7K estimated tokens per request. A
client-side pacer (`MinuteBudget`) holds a 7.6K/min budget. Token estimates are made from the
**unescaped** text. The first version measured the JSON-serialised request, which doubles every
LaTeX backslash and newline, and judged a 3.3K-token request to be 8.4K.

**Router behaviour.**

1. Check the cache under every provider first, so a rerun is free whichever provider answered.
2. On a 429 with Retry-After ≤ 65 s (a per-minute limit), wait and retry the same provider.
3. Fall back to the next provider, and pause the failing one until its reset time, when:
   - a 429 has a longer Retry-After (a daily quota)
   - Groq returns 413 (request too large)
   - 5xx errors persist
   - Groq returns `json_validate_failed`
4. If every provider is exhausted, stop the stage. Finished papers are already written, and the
   next run resumes from the cache.
5. A 401 or any other 4xx raises immediately. A bad key is a configuration error, not something to
   fall back from.

**Cache.** The key is `sha256(provider ‖ model ‖ canonical JSON of the full request)`. That
includes the system prompt, schema and generation settings, so changing any of them is a cache
miss. Only `finish_reason = stop` responses are cached: a truncated answer would otherwise replay
forever. The schema retry appends the invalid output and the validation error to the
conversation, so it is a new prompt and a fresh call, not a cached replay of the bad answer.

**Evidence verification (the hard gate).** A span is accepted if it appears in the cited passage,
or failing that in any passage of the same request, either exactly or **after removing all
whitespace from both sides**. Every non-whitespace character must match in order. The stored span
is always sliced from the source, never the model's text. Spans with fewer than 20 non-space
characters are dropped as non-identifying. A live probe returned the span "cross section". Every
rejection is recorded with its reason in the output for audit, and is never served.

The whitespace rule was tightened after an audit of real rejections. The first version only
tolerated whitespace *changed*, and 4 of 6 rejections on the fixture papers were correct quotes.
The model wrote `space.}` where the source has `space.\n}`. The other 2 rejections were genuine
misquotes:

- LaTeX quotes `` ``…'' `` turned into `"…"`
- `\@` and `~\cite{…}` dropped

Those stay rejected, as they should.

*Rejected:* fuzzy or similarity matching. It would let paraphrases through, and the SPEC calls
this a hard correctness gate.

*Rejected:* normalising LaTeX (quotes, `~`, macros) before matching. Each normalisation widens
what counts as "verbatim". If recall losses from LaTeX quote marks prove significant in the eval
harness (Phase 6), revisit with an explicit, tested list of equivalences.

## Results on the three fixture papers (extract_v1, live, 2026-10-01)

| Paper | Verified products | Rejected spans | Notes |
|---|---|---|---|
| 2112.11876 (HH→bbγγ search) | 10 | 1 (quote marks changed) | limits, likelihood scan, yields, systematics breakdown |
| 2401.05299 (ttW measurement) | 18 | 0 | inclusive and differential cross-sections, likelihood scan, yields |
| 2403.02793 (MET+jets differential) | 16 | 1 (`\cite` dropped) | differential cross-sections, migration matrices, covariance |

Precision on manual review is high. Two known weaknesses:

- **Semantic duplicates.** The same limit can be quoted from both Results and Conclusions, which
  usually land in different requests. These will be merged in Phase 4 using the embeddings that
  reconciliation computes anyway.
- **A lost exclusion limit.** The dark-matter exclusion limit in 2403.02793 was lost to a misquote
  that the gate correctly rejected.

## Cost and throughput (be realistic)

The measured cost is about 10K Groq tokens per paper (input plus output, at `medium`). Groq's
free 200K tokens/day therefore covers **about 20 papers/day**. Gemini's free daily quota adds
more, but how much is visible only in AI Studio. The first full backfill of the about 530 papers
with LaTeX therefore takes on the order of **weeks of nightly runs**, not one evening. After
that, new ATLAS papers (a few per week) fit easily. Reruns cost nothing because of the cache.
This stays within the zero-cost constraint and is the main operational trade-off of the free tier.
