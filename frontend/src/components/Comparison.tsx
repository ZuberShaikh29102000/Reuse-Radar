import { useState } from "react";
import type { PaperGap, PublishedTable } from "../api";
import { buildRows, type Row, type RowKind } from "../compare";
import { TYPE_LABEL } from "../labels";
import { latexToText } from "../latex";

const HEPDATA_ONLY_SHOWN = 10;

function TableCell({ table }: { table: PublishedTable }) {
  return (
    <div className="cell">
      <a href={table.doi_url} target="_blank" rel="noreferrer" className="cell-title">
        {table.name}
      </a>
      {table.kind === "resource" && <span className="type">{table.resource_type || "resource"}</span>}
      {table.description && (
        <p className="clamp" title={table.description}>
          {latexToText(table.description)}
        </p>
      )}
    </div>
  );
}

function ProductCell({ gap }: { gap: PaperGap }) {
  return (
    <div className="cell">
      <span className="cell-title">{gap.product.description}</span>{" "}
      <span className="type">{TYPE_LABEL[gap.product.product_type]}</span>
      <blockquote className="clamp" title={gap.product.evidence_span}>
        {gap.product.evidence_span}
      </blockquote>
    </div>
  );
}

const LINK: Record<RowKind, string> = { match: "⟷", review: "?", missing: "", hepdata_only: "" };
const LINK_LABEL: Record<RowKind, string> = {
  match: "matches",
  review: "possible match, needs review",
  missing: "",
  hepdata_only: "",
};

function CompareRow({ row, hasRecord }: { row: Row; hasRecord: boolean }) {
  return (
    <li className={`compare-row compare-${row.kind}`}>
      <div className="side hepdata">
        {row.table ? (
          <TableCell table={row.table} />
        ) : (
          <p className="empty">{hasRecord ? "Not on HEPData" : "No HEPData record"}</p>
        )}
      </div>
      <div className="connector">
        {LINK[row.kind] && (
          <span role="img" aria-label={LINK_LABEL[row.kind]}>
            {LINK[row.kind]}
          </span>
        )}
      </div>
      <div className="side paper">
        {row.gaps.length > 0 ? (
          row.gaps.map((g) => <ProductCell key={g.id} gap={g} />)
        ) : (
          <p className="empty">Nothing in the paper matched this</p>
        )}
      </div>
    </li>
  );
}

interface Props {
  gaps: PaperGap[];
  tables: PublishedTable[];
  hasRecord: boolean;
}

export function Comparison({ gaps, tables, hasRecord }: Props) {
  const [showAll, setShowAll] = useState(false);
  const { found, missing, hepdataOnly } = buildRows(gaps, tables);
  const shownOnly = showAll ? hepdataOnly : hepdataOnly.slice(0, HEPDATA_ONLY_SHOWN);

  return (
    <div className="comparison">
      <p className="compare-legend">
        <span className="key key-match">{found.filter((r) => r.kind === "match").length} matched</span>
        <span className="key key-review">
          {found.filter((r) => r.kind === "review").length +
            missing.filter((r) => r.kind === "review").length}{" "}
          to review
        </span>
        <span className="key key-missing">
          {missing.filter((r) => r.kind === "missing").length} missing from HEPData
        </span>
        <span className="key key-hepdata_only">{hepdataOnly.length} only on HEPData</span>
      </p>
      <div className="compare-head" aria-hidden="true">
        <span>On HEPData</span>
        <span />
        <span>Reported in the paper</span>
      </div>

      {found.length > 0 && (
        <section aria-label="Found on HEPData">
          <h3>Found on HEPData</h3>
          <ul className="compare">
            {found.map((r) => (
              <CompareRow key={r.key} row={r} hasRecord={hasRecord} />
            ))}
          </ul>
        </section>
      )}

      {missing.length > 0 && (
        <section aria-label="Missing from HEPData">
          <h3>Missing from HEPData</h3>
          <ul className="compare">
            {missing.map((r) => (
              <CompareRow key={r.key} row={r} hasRecord={hasRecord} />
            ))}
          </ul>
        </section>
      )}

      {hepdataOnly.length > 0 && (
        <section aria-label="Only on HEPData">
          <h3>Only on HEPData</h3>
          <p className="muted">
            Tables that nothing extracted from the paper matched. These often point to results
            the extractor missed.
          </p>
          <ul className="compare">
            {shownOnly.map((r) => (
              <CompareRow key={r.key} row={r} hasRecord={hasRecord} />
            ))}
          </ul>
          {hepdataOnly.length > HEPDATA_ONLY_SHOWN && (
            <button type="button" className="link" onClick={() => setShowAll(!showAll)}>
              {showAll ? "Show fewer" : `Show all ${hepdataOnly.length}`}
            </button>
          )}
        </section>
      )}
    </div>
  );
}
