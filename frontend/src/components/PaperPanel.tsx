import { useEffect, useRef, useState } from "react";
import { api, type Paper, type PaperGap } from "../api";
import { latexToText } from "../latex";
import { GapRow } from "./GapRow";

interface Props {
  inspireId: number;
  token: string;
  onClose: () => void;
}

export function PaperPanel({ inspireId, token, onClose }: Props) {
  const [data, setData] = useState<{ paper: Paper; gaps: PaperGap[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    dialog.current?.showModal?.();
    let cancelled = false;
    api
      .paper(inspireId)
      .then((d) => !cancelled && setData(d))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [inspireId]);

  const counts = data?.gaps.reduce<Record<string, number>>((acc, g) => {
    acc[g.status] = (acc[g.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <dialog ref={dialog} className="paper-panel" onClose={onClose} aria-labelledby="paper-title">
      <button type="button" className="close" onClick={onClose} aria-label="Close">
        ×
      </button>
      {error && <p role="alert" className="error">{error}</p>}
      {!data && !error && <p aria-busy="true">Loading…</p>}
      {data && (
        <>
          <h2 id="paper-title">{latexToText(data.paper.title)}</h2>
          <p className="muted">
            INSPIRE {data.paper.inspire_id} · {data.paper.year} · {data.paper.collaboration} ·
            readiness <strong>{data.paper.readiness_score ?? "–"}/100</strong>
          </p>
          <p className="links">
            <a href={data.paper.inspire_url} target="_blank" rel="noreferrer">INSPIRE</a>
            {data.paper.arxiv_url && (
              <a href={data.paper.arxiv_url} target="_blank" rel="noreferrer">arXiv</a>
            )}
            {data.paper.hepdata_url ? (
              <a href={data.paper.hepdata_url} target="_blank" rel="noreferrer">HEPData record</a>
            ) : (
              <span className="muted">No HEPData record</span>
            )}
          </p>
          <p>
            {data.gaps.length} declared data products:{" "}
            {Object.entries(counts ?? {})
              .map(([status, n]) => `${n} ${status.replace("_", " ")}`)
              .join(", ")}
          </p>
          <ul className="gaps">
            {data.gaps.map((g) => (
              <GapRow key={g.id} gap={g} token={token} />
            ))}
          </ul>
        </>
      )}
    </dialog>
  );
}
