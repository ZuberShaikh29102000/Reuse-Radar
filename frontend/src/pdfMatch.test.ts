import { describe, expect, it } from "vitest";
import { assignItems, locate, locateInDocument, normaliseItems, normaliseQuote } from "./pdfMatch";

describe("assignItems", () => {
  it("gives a text item two quotes share to the quote covering more of it", () => {
    // Seen live: a caption line held the end of one quote and the first word of the next.
    const page = normaliseItems([
      "Table 5: The observed upper limit on the visible",
      "cross-section in the signal regions used in the analysis. The",
      "expected yields for a few signal points are listed for reference.",
    ]);
    const first = locate(normaliseQuote("The observed upper limit on the visible cross-section in the signal regions used in the analysis."), page);
    const second = locate(normaliseQuote("The expected yields for a few signal points are listed for reference."), page);
    expect(first && second).toBeTruthy();
    const owners = assignItems(page, [
      { key: "first", range: first! },
      { key: "second", range: second! },
    ]);
    expect([...owners].sort()).toEqual([
      [0, "first"],
      [1, "first"],
      [2, "second"],
    ]);
  });
});

describe("normaliseQuote", () => {
  it("drops LaTeX commands, maths markup, escapes and references", () => {
    expect(normaliseQuote(String.raw`Lower limits at 95$\%$ CL on $\Lambda$ (see Figure~\ref{fig:lim})`)).toBe(
      "lowerlimitsat95clonseefigure",
    );
    expect(normaliseQuote(String.raw`$\sigma_\textrm{vis}\times\mathcal{B}$`)).toBe("visb");
  });
});

describe("locate", () => {
  // How pdf.js reports a caption: fragments split at line breaks, a hyphenated word, a ligature,
  // rendered symbols and a reference number the LaTeX source does not contain.
  const items = [
    "Figure 4: Lower limits at 95% CL on Λ for the dimuon chan-",
    "nel for different signal chiralities in the con-",
    "structive interference SRs of the analysis, see Table 3. The ﬁt uses",
    "other text on the page",
  ];
  const page = normaliseItems(items);

  it("finds a quote across line breaks, hyphenation, symbols and ligatures", () => {
    const quote = normaliseQuote(
      String.raw`Lower limits at 95$\%$ CL on $\Lambda$ for the dimuon channel for different signal chiralities`,
    );
    expect(locate(quote, page)).toMatchObject({ first: 0, last: 1 });
    expect(locate(normaliseQuote("The fit uses other text"), page)).toMatchObject({ first: 2, last: 3 });
  });

  it("falls back to the first and last words when a reference breaks the middle", () => {
    const quote = normaliseQuote(
      String.raw`signal chiralities in the constructive interference SRs of the analysis, see Table~\ref{tab:x}. The fit uses`,
    );
    expect(locate(quote, page)).toMatchObject({ first: 1, last: 2 });
  });

  it("refuses quotes too short to identify and text that is not there", () => {
    expect(locate(normaliseQuote("Table 3"), page)).toBeNull();
    expect(locate(normaliseQuote("A sentence that appears nowhere in this document at all"), page)).toBeNull();
  });
});

describe("locateInDocument", () => {
  it("reports the first page that holds the quote", () => {
    const pages = [normaliseItems(["Introduction text only"]), normaliseItems(["Observed and expected limits on the mass"])];
    expect(locateInDocument("Observed and expected limits on the mass", pages)).toMatchObject({
      page: 2,
      range: { first: 0, last: 0 },
    });
  });
});
