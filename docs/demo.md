# Five-minute demo

A walkthrough of the live system, in the order that tells the story. Open the API docs a minute
beforehand: Render's free plan sleeps when idle and the first request takes about 50 seconds.

| What | Link |
|---|---|
| Curator UI | <https://reuse-radar.work-zubershaikh.workers.dev> |
| API docs | <https://reuse-radar-api.onrender.com/api/docs> |
| Code | <https://github.com/ZuberShaikh29102000/Reuse-Radar> |

## 1. The problem (30 s)

Particle-physics papers publish results as plots and tables. The numbers behind them belong in
HEPData, the field's public archive, so others can reuse them. Uploading is voluntary and often
partial, and nobody knows which results are missing: finding out means reading every paper.

## 2. The triage queue (1 min)

Open the curator UI.

- **Stats bar.** 624 ATLAS papers harvested; the analysed ones list their declared data products,
  and "Open gaps" counts the products not confirmed on HEPData.
- **Queue.** One row per gap, most urgent first. Severity starts from the product type (a
  likelihood or a covariance matrix is hard to reconstruct from a plot) and drops a level when
  the HEPData match is uncertain or the extractor was unsure.
- **Filters.** Tick *Likelihood* and set severity to *High only*: these are the results a
  reinterpretation study cannot do without.

## 3. The evidence (1 min, the key point)

Click the ▸ on a row.

- **Evidence.** A verbatim quote from the paper's LaTeX source. The extractor must quote the paper,
  and the pipeline checks the quote character for character (whitespace aside). Claims without a
  matching quote are dropped, so the model cannot invent a product.
- **Closest HEPData item.** The best candidate table in the paper's HEPData record, with the
  embedding similarity, caption overlap and combined score. Every number behind a decision is
  stored, so a curator can see *why* something was called missing.

Click the paper title: the panel shows the whole paper's profile and its readiness score (the
weighted share of declared products already on HEPData; uncertain matches count half).

## 4. Curator review (30 s)

Paste the curator token into **Curator token**, press **Use**, and **Accept** or **Reject** a gap.
Reviews are stored with the reviewer's name, are never deleted by pipeline reruns, and become
labelled data for measuring the extractor.

## 5. For programs and AI agents (1 min)

- **API docs.** Try `GET /api/gaps` with `min_severity=2`, or `GET /api/search?q=cross section`.
  The API is read-only and calls no model: it serves precomputed rows.
- **MCP server.** `find_reuse_gaps` and `get_reuse_profile` let an AI assistant ask "which ATLAS
  2023 likelihoods are missing from HEPData?" (setup: `docs/mcp.md`).

## 6. How it is built (1 min)

```
INSPIRE → harvest → filter (LaTeX sections) → extract (LLM + verified quotes)
        → reconcile (HEPData via DataCite, local embeddings) → Postgres → API → UI / MCP
```

- Free tiers only. Rate limits are respected for every upstream API, every LLM response is cached,
  and there is no model in the request path.
- **Measured quality.** On hand-labelled papers: precision 0.91, recall 0.65, gap detection
  precision 1.00. The known weakness is data-versus-prediction plots and yield tables, documented
  and targeted by the next prompt version.
- 235 Python tests and 7 frontend tests, plus a replayed evaluation gate, all run in CI on every
  push. Each design decision is in `docs/adr/`.

## Likely questions

- **Why not scrape hepdata.net?** It answers scripts with a Cloudflare browser check, which we do
  not bypass. HEPData records are read through their DataCite DOIs instead.
- **Why only some papers so far?** The free LLM tier allows about 20 papers a day. The rest are
  queued for a GPU batch run; the code path is the same.
- **Can the AI hallucinate a gap?** It can miss products (recall 0.65), but every listed product
  carries a quote checked against the source. A wrong match is possible; that is what the
  *Needs review* status and curator reviews are for.
