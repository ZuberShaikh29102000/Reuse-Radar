import { API_DOCS_URL } from "../api";
import { STATUS_HELP, STATUS_LABEL } from "../labels";

const REPO = "https://github.com/ZuberShaikh29102000/Reuse-Radar";
const STATUSES = ["missing", "no_record", "uncertain", "published"] as const;

// A plain-language introduction for visitors who are not HEPData curators. Curators can hide
// it; the choice is remembered per browser (see App).
export function Intro({ onHide }: { onHide: () => void }) {
  return (
    <section className="intro" aria-labelledby="intro-title">
      <button type="button" className="close" onClick={onHide} aria-label="Hide introduction">
        ×
      </button>
      <h2 id="intro-title">Finding particle-physics results that were never made reusable</h2>
      <p className="lead">
        LHC papers report their results (measurements, limits, detector efficiencies) as plots
        and tables. The numbers behind them belong in{" "}
        <a href="https://www.hepdata.net" target="_blank" rel="noreferrer">
          HEPData
        </a>
        , the field's public archive, so other scientists can reuse them. Many never get there,
        and nobody knows which ones. Reuse Radar finds them.
      </p>

      <ol className="steps">
        <li>
          <strong>Collect</strong> 624 ATLAS papers (2020–2025) from INSPIRE.
        </li>
        <li>
          <strong>Read</strong> each paper with an AI model that lists the results it reports.
          Every item must quote the paper word for word, and the quote is checked automatically,
          so the model cannot invent results.
        </li>
        <li>
          <strong>Compare</strong> each result with the paper's HEPData record.
        </li>
        <li>
          <strong>Rank</strong> what is missing for curators, most urgent first.
        </li>
      </ol>

      <div className="legend">
        <span className="muted">Reading the list:</span>
        {STATUSES.map((s) => (
          <span key={s}>
            <span className={`status status-${s}`}>{STATUS_LABEL[s]}</span> {STATUS_HELP[s]}
          </span>
        ))}
        <span>Click ▸ on a row to see the quote from the paper and the closest HEPData table.</span>
      </div>

      <p className="stack">
        <strong>Built with</strong> Python data pipeline · LLM extraction with evidence
        verification · embedding-based matching · Django REST API · PostgreSQL + pgvector · React
        + TypeScript · automated tests and an extraction-quality gate in CI · free-tier hosting.
      </p>

      <p className="links">
        <a href={REPO} target="_blank" rel="noreferrer">
          Source code
        </a>
        <a href={API_DOCS_URL} target="_blank" rel="noreferrer">
          API documentation
        </a>
        <a href={`${REPO}/blob/main/docs/demo.md`} target="_blank" rel="noreferrer">
          5-minute walkthrough
        </a>
        <span>
          Built by <strong>Zuber Shaikh</strong>
        </span>
      </p>

      <p className="disclaimer muted">
        Independent project, not affiliated with or endorsed by CERN, the ATLAS Collaboration or
        HEPData. Results are produced automatically and can contain mistakes: on hand-labelled
        papers, 91% of listed results are correct and about two thirds of all results are found.
      </p>
    </section>
  );
}
