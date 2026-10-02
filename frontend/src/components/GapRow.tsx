import { useId, useState } from "react";
import { api, ApiError, type Gap, type LatestReview, type PaperGap } from "../api";
import { SEVERITY_LABEL, STATUS_HELP, STATUS_LABEL, TYPE_LABEL } from "../labels";
import { latexToText } from "../latex";

interface Props {
  gap: Gap | PaperGap;
  token: string;
  onOpenPaper?: (inspireId: number) => void;
}

export function SeverityBadge({ severity }: { severity: number }) {
  return (
    <span className={`badge sev-${severity}`} title={`Severity ${severity}`}>
      {SEVERITY_LABEL[severity] ?? severity}
    </span>
  );
}

export function GapRow({ gap, token, onOpenPaper }: Props) {
  const [open, setOpen] = useState(false);
  const [review, setReview] = useState<LatestReview | null>(gap.product.latest_review);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const detailsId = useId();
  const paper = "paper" in gap ? gap.paper : null;

  async function submit(verdict: "accept" | "reject") {
    setBusy(true);
    setError(null);
    try {
      const saved = await api.review(token, gap.product.id, verdict, note);
      setReview({ verdict: saved.verdict, reviewer: saved.reviewer, created_at: saved.created_at });
      setNote("");
    } catch (e) {
      setError(
        e instanceof ApiError && (e.status === 401 || e.status === 403)
          ? "Your curator token was rejected."
          : `Could not save the review: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <li className={`gap gap--${gap.status}`}>
      <div className="gap-summary">
        <SeverityBadge severity={gap.severity} />
        <span className="type">{TYPE_LABEL[gap.product.product_type]}</span>
        <button
          type="button"
          className="expander"
          aria-expanded={open}
          aria-controls={detailsId}
          onClick={() => setOpen(!open)}
        >
          {gap.product.description}
        </button>
        <span className={`status status-${gap.status}`} title={STATUS_HELP[gap.status]}>
          {STATUS_LABEL[gap.status]}
        </span>
        {review && (
          <span className={`review ${review.verdict}`}>
            {review.verdict === "accept" ? "Accepted" : "Rejected"} by {review.reviewer}
          </span>
        )}
      </div>
      {paper && (
        <div className="gap-paper">
          <button type="button" className="link" onClick={() => onOpenPaper?.(paper.inspire_id)}>
            {latexToText(paper.title)}
          </button>{" "}
          <span className="muted">
            ({paper.year}, readiness {paper.readiness_score ?? "–"}/100)
          </span>
        </div>
      )}
      {open && (
        <div className="gap-details" id={detailsId}>
          <p className="muted">
            Evidence from <strong>{gap.product.evidence_section}</strong> ({gap.product.evidence_kind}
            ), copied verbatim from the paper's LaTeX:
          </p>
          <pre className="evidence">{gap.product.evidence_span}</pre>
          <dl className="facts">
            <dt>Extraction confidence</dt>
            <dd>{Math.round(gap.product.confidence * 100)}%</dd>
            {gap.matched_table && (
              <>
                <dt>Closest HEPData item</dt>
                <dd>
                  <a href={gap.matched_table.doi_url} target="_blank" rel="noreferrer">
                    {gap.matched_table.name}
                  </a>{" "}
                  <span className="muted">({gap.matched_table.kind})</span>
                </dd>
              </>
            )}
            {gap.match && (
              <>
                <dt>Match signals</dt>
                <dd>
                  meaning {gap.match.embedding_similarity.toFixed(2)} · caption overlap{" "}
                  {gap.match.caption_overlap.toFixed(2)} · score {gap.match.score.toFixed(2)}
                </dd>
              </>
            )}
            {gap.product.merged_duplicates > 0 && (
              <>
                <dt>Merged duplicates</dt>
                <dd>{gap.product.merged_duplicates}</dd>
              </>
            )}
          </dl>
          {paper && (
            <p className="links">
              <a href={paper.inspire_url} target="_blank" rel="noreferrer">INSPIRE</a>
              {paper.arxiv_url && (
                <a href={paper.arxiv_url} target="_blank" rel="noreferrer">arXiv</a>
              )}
              {paper.hepdata_url && (
                <a href={paper.hepdata_url} target="_blank" rel="noreferrer">HEPData record</a>
              )}
            </p>
          )}
          {token ? (
            <div className="review-box">
              <label>
                Note (optional){" "}
                <input
                  value={note}
                  maxLength={2000}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="e.g. table exists under a different name"
                />
              </label>
              <button type="button" disabled={busy} onClick={() => void submit("accept")}>
                Accept
              </button>
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void submit("reject")}
              >
                Reject
              </button>
              {error && (
                <p role="alert" className="error">
                  {error}
                </p>
              )}
            </div>
          ) : (
            <p className="muted">Add your curator token (top right) to accept or reject.</p>
          )}
        </div>
      )}
    </li>
  );
}
