import type { PaperGap, PublishedTable } from "./api";

// Rows of the side-by-side view of one paper: HEPData's record on the left, what the paper
// reports on the right. Matched pairs share a row; gaps have no left side; HEPData tables that
// nothing in the paper matched have no right side (often results the extractor missed).

export type RowKind = "match" | "review" | "missing" | "hepdata_only";

export interface Row {
  key: string;
  kind: RowKind;
  table: PublishedTable | null;
  gaps: PaperGap[];
}

export interface CompareRows {
  /** HEPData tables with at least one paper result linked to them, in HEPData's order. */
  found: Row[];
  /** Paper results with no HEPData table (missing, no record, or an unlinked review). */
  missing: Row[];
  /** HEPData tables that no paper result matched. */
  hepdataOnly: Row[];
}

export function buildRows(gaps: PaperGap[], tables: PublishedTable[]): CompareRows {
  const byDoi = new Map(tables.map((t) => [t.table_doi, [] as PaperGap[]]));
  const unmatched: PaperGap[] = [];
  for (const gap of gaps) {
    // A "missing" gap may carry its best (rejected) candidate table; only published and
    // uncertain gaps are linked to a table.
    const linked = gap.status === "published" || gap.status === "uncertain";
    const rows = linked && gap.matched_table ? byDoi.get(gap.matched_table.table_doi) : undefined;
    if (rows) rows.push(gap);
    else unmatched.push(gap);
  }
  const found: Row[] = [];
  const hepdataOnly: Row[] = [];
  for (const table of tables) {
    const matched = byDoi.get(table.table_doi) ?? [];
    if (matched.length === 0) {
      hepdataOnly.push({ key: table.table_doi, kind: "hepdata_only", table, gaps: [] });
    } else {
      const kind = matched.some((g) => g.status === "published") ? "match" : "review";
      found.push({ key: table.table_doi, kind, table, gaps: matched });
    }
  }
  const missing: Row[] = unmatched.map((g) => ({
    key: `gap-${g.id}`,
    kind: g.status === "uncertain" ? "review" : "missing",
    table: null,
    gaps: [g],
  }));
  return { found, missing, hepdataOnly };
}
