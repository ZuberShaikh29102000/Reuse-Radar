import { useEffect, useRef, useState } from "react";
import { api, type PaperDetail } from "../api";
import { latexToText } from "../latex";
import { Comparison } from "./Comparison";
import { GapRow } from "./GapRow";

interface Props {
  inspireId: number;
  token: string;
  onClose: () => void;
}

export function PaperPanel({ inspireId, token, onClose }: Props) {
  const [data, setData] = useState<PaperDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<"compare" | "list">("compare");
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

  const canCompare = Array.isArray(data?.hepdata_tables);
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
          {/* An API deployed before hepdata_tables existed omits it; without the table list the
              comparison would show every product as missing, so only the list is offered. */}
          {canCompare && (
            <div className="view-switch" role="group" aria-label="View">
              <button
                type="button"
                aria-pressed={view === "compare"}
                onClick={() => setView("compare")}
              >
                Side by side with HEPData
              </button>
              <button type="button" aria-pressed={view === "list"} onClick={() => setView("list")}>
                List and review
              </button>
            </div>
          )}
          {canCompare && view === "compare" ? (
            <Comparison
              gaps={data.gaps}
              tables={data.hepdata_tables}
              hasRecord={Boolean(data.paper.hepdata_url)}
            />
          ) : (
            <ul className="gaps">
              {data.gaps.map((g) => (
                <GapRow key={g.id} gap={g} token={token} />
              ))}
            </ul>
          )}
        </>
      )}
    </dialog>
  );
}
