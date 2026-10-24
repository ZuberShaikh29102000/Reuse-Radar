import { describe, expect, it } from "vitest";
import type { GapStatus, PaperGap, PublishedTable } from "./api";
import { buildRows } from "./compare";

function table(n: number): PublishedTable {
  const table_doi = `10.17182/hepdata.1.v1/t${n}`;
  return {
    table_doi,
    name: `Table ${n}`,
    description: "",
    kind: "table",
    resource_type: "",
    doi_url: `https://doi.org/${table_doi}`,
  };
}

function gap(id: number, status: GapStatus, matched: PublishedTable | null): PaperGap {
  return {
    id,
    status,
    severity: status === "published" ? 0 : 2,
    product: {
      id,
      product_type: "upper_limit",
      description: `Product ${id}`,
      evidence_span: "quote",
      evidence_section: "Results",
      evidence_kind: "caption",
      confidence: 0.9,
      merged_duplicates: 0,
      extraction_version: "extract_v1+filter_1",
      latest_review: null,
    },
    matched_table: matched,
    match: null,
  };
}

describe("buildRows", () => {
  const [t1, t2, t3] = [table(1), table(2), table(3)];

  it("pairs matches, separates gaps and keeps unmatched HEPData tables", () => {
    const rows = buildRows(
      [gap(1, "published", t1), gap(2, "uncertain", t2), gap(3, "missing", t3), gap(4, "no_record", null)],
      [t1, t2, t3],
    );
    expect(rows.found.map((r) => [r.table?.name, r.kind])).toEqual([
      ["Table 1", "match"],
      ["Table 2", "review"],
    ]);
    // A missing product's best candidate was rejected, so Table 3 counts as HEPData-only.
    expect(rows.missing.map((r) => [r.gaps[0]?.id, r.kind])).toEqual([
      [3, "missing"],
      [4, "missing"],
    ]);
    expect(rows.hepdataOnly.map((r) => r.table?.name)).toEqual(["Table 3"]);
  });

  it("groups several products matched to one table, and a published one wins", () => {
    const rows = buildRows([gap(1, "uncertain", t1), gap(2, "published", t1)], [t1]);
    expect(rows.found).toHaveLength(1);
    expect(rows.found[0]?.kind).toBe("match");
    expect(rows.found[0]?.gaps.map((g) => g.id)).toEqual([1, 2]);
  });

  it("puts a review whose table is not in the record on the paper side only", () => {
    const rows = buildRows([gap(1, "uncertain", t2)], [t1]);
    expect(rows.missing.map((r) => r.kind)).toEqual(["review"]);
    expect(rows.hepdataOnly).toHaveLength(1);
  });
});
