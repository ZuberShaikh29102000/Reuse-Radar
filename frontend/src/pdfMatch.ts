// Locate an evidence quote (stored verbatim from the paper's LaTeX source) inside the text of
// the typeset PDF, as pdf.js reports it: one string per text item.
//
// The two never match character for character: LaTeX has commands, maths and escapes; the PDF
// has rendered symbols, ligatures and hyphenated line breaks. Both sides are therefore reduced to
// lowercase ASCII letters and digits only. Greek letters and other symbols drop out on both sides
// (\Lambda in the source, Λ in the PDF), which is what makes the comparison work. References
// (\ref, \cite) render as numbers the source does not contain, so a quote that does not match
// whole is located by its first and last 40 characters instead.

const ANCHOR = 40;
const MIN_QUOTE = 15;

/** The quote as comparable text: no LaTeX, letters and digits only. */
export function normaliseQuote(latex: string): string {
  return latex
    .replace(/\\(?:cite[a-z]*|ref|eqref|autoref|cref|Cref|label|url|footnote)\*?\{[^{}]*\}/g, "")
    .replace(/\\[A-Za-z]+\*?/g, "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}

export interface ItemText {
  /** Letters and digits of all items, concatenated. */
  text: string;
  /** For each character of `text`, the index of the item it came from. */
  owner: number[];
}

export function normaliseItems(items: readonly string[]): ItemText {
  let text = "";
  const owner: number[] = [];
  items.forEach((item, index) => {
    for (const ch of item.normalize("NFKC").toLowerCase()) {
      if (/[a-z0-9]/.test(ch)) {
        text += ch;
        owner.push(index);
      }
    }
  });
  return { text, owner };
}

export interface ItemRange {
  first: number;
  last: number;
  /** The matched span of the normalised page text: [start, end). */
  start: number;
  end: number;
}

/** The range of text items that holds the quote, or null if it cannot be found on this page. */
export function locate(quote: string, page: ItemText): ItemRange | null {
  if (quote.length < MIN_QUOTE) return null;
  const range = (start: number, end: number): ItemRange => {
    const stop = Math.min(end, page.text.length);
    return { first: page.owner[start] ?? 0, last: page.owner[stop - 1] ?? 0, start, end: stop };
  };

  const whole = page.text.indexOf(quote);
  if (whole >= 0) return range(whole, whole + quote.length);
  if (quote.length <= ANCHOR) return null;

  const head = page.text.indexOf(quote.slice(0, ANCHOR));
  if (head >= 0) {
    const tail = page.text.indexOf(quote.slice(-ANCHOR), head + ANCHOR);
    // The tail must be roughly where the quote's length says; rendered references add a little.
    if (tail >= 0 && tail + ANCHOR - head <= quote.length * 1.3 + 40) {
      return range(head, tail + ANCHOR);
    }
    return range(head, head + quote.length);
  }
  const tail = page.text.indexOf(quote.slice(-ANCHOR));
  if (tail >= 0) return range(Math.max(0, tail + ANCHOR - quote.length), tail + ANCHOR);
  return null;
}

/**
 * Which quote each text item belongs to. pdf.js items are runs of words, so two quotes can share
 * one; it goes to the quote that covers more of its characters.
 */
export function assignItems<K>(page: ItemText, quotes: { key: K; range: ItemRange }[]): Map<number, K> {
  const best = new Map<number, { key: K; chars: number }>();
  for (const { key, range } of quotes) {
    const chars = new Map<number, number>();
    for (let i = range.start; i < range.end; i++) {
      const item = page.owner[i];
      if (item !== undefined) chars.set(item, (chars.get(item) ?? 0) + 1);
    }
    for (const [item, count] of chars) {
      const current = best.get(item);
      if (!current || count > current.chars) best.set(item, { key, chars: count });
    }
  }
  return new Map([...best].map(([item, { key }]) => [item, key]));
}

export interface PdfLocation {
  /** 1-based page number. */
  page: number;
  range: ItemRange;
}

/** The first page (and item range) where the quote occurs. */
export function locateInDocument(latex: string, pages: readonly ItemText[]): PdfLocation | null {
  const quote = normaliseQuote(latex);
  for (let i = 0; i < pages.length; i++) {
    const page = pages[i];
    if (!page) continue;
    const range = locate(quote, page);
    if (range) return { page: i + 1, range };
  }
  return null;
}
