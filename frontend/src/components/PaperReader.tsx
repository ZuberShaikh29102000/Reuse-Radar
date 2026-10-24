import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import type { PaperGap, PublishedTable } from "../api";
import { STATUS_LABEL, TYPE_LABEL } from "../labels";
import { latexToText } from "../latex";
import {
  assignItems,
  locate,
  locateInDocument,
  normaliseItems,
  normaliseQuote,
  type PdfLocation,
} from "../pdfMatch";

// The original paper (the PDF, loaded by the visitor's browser straight from arXiv, which
// serves PDFs with Access-Control-Allow-Origin: *) with every extracted result highlighted where
// the paper states it, next to the HEPData record. Nothing is re-hosted.

type PdfJs = typeof import("pdfjs-dist");
type Kind = "on" | "review" | "missing";

let pdfjsPromise: Promise<PdfJs> | null = null;
function loadPdfJs(): Promise<PdfJs> {
  // Loaded on first use only: pdf.js is large and most visits never open a paper.
  pdfjsPromise ??= Promise.all([
    import("pdfjs-dist"),
    import("pdfjs-dist/build/pdf.worker.min.mjs?url"),
  ]).then(([pdfjs, worker]) => {
    pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
    return pdfjs;
  });
  return pdfjsPromise;
}

function kindOf(gap: PaperGap): Kind {
  if (gap.status === "published") return "on";
  if (gap.status === "uncertain") return "review";
  return "missing";
}

interface PageSize {
  width: number;
  height: number;
}

interface Highlight {
  gapId: number;
  quote: string;
  kind: Kind;
}

interface PageProps {
  doc: PDFDocumentProxy;
  number: number;
  size: PageSize;
  scale: number;
  highlights: Highlight[];
  onRendered: (number: number) => void;
}

function PdfPage({ doc, number, size, scale, highlights, onRendered }: PageProps) {
  const holder = useRef<HTMLDivElement>(null);
  const done = useRef(false);

  useEffect(() => {
    const element = holder.current;
    if (!element) return;
    let cancelled = false;
    const draw = async () => {
      done.current = true;
      const pdfjs = await loadPdfJs();
      const page = await doc.getPage(number);
      const viewport = page.getViewport({ scale });
      const ratio = window.devicePixelRatio || 1;
      const canvas = document.createElement("canvas");
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      await page.render({
        canvas,
        viewport,
        transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
      }).promise;
      const layer = document.createElement("div");
      layer.className = "textLayer";
      layer.style.setProperty("--total-scale-factor", String(scale));
      const text = new pdfjs.TextLayer({
        textContentSource: await page.getTextContent(),
        container: layer,
        viewport,
      });
      await text.render();
      if (cancelled) return;
      // Locate again on the rendered layer, whose strings line up one-to-one with its elements.
      const items = normaliseItems(text.textContentItemsStr);
      const found = highlights.flatMap((h) => {
        const range = locate(h.quote, items);
        return range ? [{ key: h, range }] : [];
      });
      for (const [index, h] of assignItems(items, found)) {
        const div = text.textDivs[index];
        if (!div || !div.textContent?.trim()) continue;
        div.classList.add("hl", `hl-${h.kind}`);
        div.dataset.gap = String(h.gapId);
      }
      element.replaceChildren(canvas, layer);
      onRendered(number);
    };
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting) && !done.current) {
          observer.disconnect();
          void draw();
        }
      },
      { rootMargin: "600px 0px" },
    );
    observer.observe(element);
    return () => {
      cancelled = true;
      observer.disconnect();
    };
  }, [doc, number, scale, highlights, onRendered]);

  return (
    <div
      ref={holder}
      className="pdf-page"
      data-page={number}
      style={{ width: size.width * scale, height: size.height * scale }}
    />
  );
}

interface Props {
  arxivId: string;
  gaps: PaperGap[];
  tables: PublishedTable[];
}

export function PaperReader({ arxivId, gaps, tables }: Props) {
  const pdfUrl = `https://arxiv.org/pdf/${arxivId}`;
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [sizes, setSizes] = useState<PageSize[]>([]);
  const [locations, setLocations] = useState<Map<number, PdfLocation>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [scale, setScale] = useState(1);
  const [renderedTick, setRenderedTick] = useState(0);
  const pdfPane = useRef<HTMLDivElement>(null);
  const pendingScroll = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    let task: ReturnType<PdfJs["getDocument"]> | null = null;
    (async () => {
      const pdfjs = await loadPdfJs();
      if (cancelled) return;
      task = pdfjs.getDocument({ url: pdfUrl });
      const loaded = await task.promise;
      const pageSizes: PageSize[] = [];
      const texts = [];
      for (let n = 1; n <= loaded.numPages; n++) {
        const page = await loaded.getPage(n);
        const { width, height } = page.getViewport({ scale: 1 });
        pageSizes.push({ width, height });
        const content = await page.getTextContent();
        texts.push(normaliseItems(content.items.flatMap((item) => ("str" in item ? [item.str] : []))));
      }
      const found = new Map<number, PdfLocation>();
      for (const gap of gaps) {
        const where = locateInDocument(gap.product.evidence_span, texts);
        if (where) found.set(gap.id, where);
      }
      if (cancelled) return;
      const paneWidth = (pdfPane.current?.clientWidth ?? 800) - 24;
      const widest = Math.max(...pageSizes.map((s) => s.width));
      setScale(Math.min(2, Math.max(0.5, paneWidth / widest)));
      setSizes(pageSizes);
      setLocations(found);
      setDoc(loaded);
    })().catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
      void task?.destroy();
    };
  }, [pdfUrl, gaps]);

  const byPage = useMemo(() => {
    const map = new Map<number, Highlight[]>();
    for (const gap of gaps) {
      const where = locations.get(gap.id);
      if (!where) continue;
      const list = map.get(where.page) ?? [];
      list.push({ gapId: gap.id, quote: normaliseQuote(gap.product.evidence_span), kind: kindOf(gap) });
      map.set(where.page, list);
    }
    return map;
  }, [gaps, locations]);

  const onRendered = useCallback(() => setRenderedTick((t) => t + 1), []);

  // Mark the selected result in the paper, and scroll to it once its page is drawn.
  useEffect(() => {
    const pane = pdfPane.current;
    if (!pane) return;
    const marks = Array.from(pane.querySelectorAll<HTMLElement>("[data-gap]"));
    for (const el of marks) el.classList.toggle("hl-selected", el.dataset.gap === String(selected));
    const first = marks.find((el) => el.dataset.gap === String(selected));
    if (pendingScroll.current !== null && pendingScroll.current === selected && first) {
      first.scrollIntoView({ block: "center", behavior: "smooth" });
      pendingScroll.current = null;
    }
  }, [selected, renderedTick]);

  const show = (gapId: number) => {
    setSelected(gapId);
    pendingScroll.current = gapId;
    const where = locations.get(gapId);
    const pageEl = pdfPane.current?.querySelector<HTMLElement>(`[data-page="${where?.page}"]`);
    if (pageEl && !pageEl.querySelector(`[data-gap="${gapId}"]`)) {
      pageEl.scrollIntoView({ block: "start" }); // drawing it triggers the precise scroll
    }
    setRenderedTick((t) => t + 1);
  };

  const linked = new Map<string, PaperGap[]>();
  const unlinked: PaperGap[] = [];
  for (const gap of gaps) {
    const doi = gap.matched_table?.table_doi;
    if (kindOf(gap) !== "missing" && doi && tables.some((t) => t.table_doi === doi)) {
      linked.set(doi, [...(linked.get(doi) ?? []), gap]);
    } else {
      unlinked.push(gap);
    }
  }
  const matchedTables = tables.filter((t) => linked.has(t.table_doi));
  const otherTables = tables.filter((t) => !linked.has(t.table_doi));
  const selectedGap = gaps.find((g) => g.id === selected) ?? null;
  const notFound = gaps.filter((g) => doc && !locations.has(g.id));

  return (
    <div className="reader">
      <aside className="reader-side" aria-label="HEPData record">
        <p className="compare-legend">
          <span className="key key-match">on HEPData</span>
          <span className="key key-review">needs review</span>
          <span className="key key-missing">missing from HEPData</span>
        </p>
        {selectedGap && (
          <div className={`picked picked-${kindOf(selectedGap)}`} aria-live="polite">
            <strong>{selectedGap.product.description}</strong>{" "}
            <span className="type">{TYPE_LABEL[selectedGap.product.product_type]}</span>
            <p>
              {STATUS_LABEL[selectedGap.status]}
              {kindOf(selectedGap) !== "missing" && selectedGap.matched_table && (
                <>
                  {": "}
                  <a href={selectedGap.matched_table.doi_url} target="_blank" rel="noreferrer">
                    {selectedGap.matched_table.name}
                  </a>
                </>
              )}
            </p>
          </div>
        )}

        <h3>On HEPData</h3>
        {tables.length === 0 && <p className="muted">This paper has no HEPData record.</p>}
        <ul className="side-list">
          {matchedTables.map((t) => {
            const its = linked.get(t.table_doi) ?? [];
            const kind = its.some((g) => kindOf(g) === "on") ? "on" : "review";
            const active = its.some((g) => g.id === selected);
            return (
              <li key={t.table_doi}>
                <button
                  type="button"
                  className={`side-item side-${kind}${active ? " active" : ""}`}
                  onClick={() => its[0] && show(its[0].id)}
                >
                  <span className="cell-title">{t.name}</span>
                  {t.description && <span className="clamp">{latexToText(t.description)}</span>}
                </button>
              </li>
            );
          })}
        </ul>
        {otherTables.length > 0 && (
          <details>
            <summary>
              {otherTables.length} more HEPData tables that nothing in the paper matched
            </summary>
            <ul className="side-list">
              {otherTables.map((t) => (
                <li key={t.table_doi}>
                  <a href={t.doi_url} target="_blank" rel="noreferrer" className="side-item side-none">
                    <span className="cell-title">{t.name}</span>
                    {t.description && <span className="clamp">{latexToText(t.description)}</span>}
                  </a>
                </li>
              ))}
            </ul>
          </details>
        )}

        {unlinked.length > 0 && (
          <>
            <h3>Missing from HEPData</h3>
            <ul className="side-list">
              {unlinked.map((g) => (
                <li key={g.id}>
                  <button
                    type="button"
                    className={`side-item side-${kindOf(g)}${g.id === selected ? " active" : ""}`}
                    onClick={() => show(g.id)}
                    disabled={Boolean(doc) && !locations.has(g.id)}
                  >
                    <span className="cell-title">{g.product.description}</span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
        {notFound.length > 0 && (
          <p className="muted small">
            {notFound.length} of {gaps.length} quotes could not be located in the PDF's text
            (equations or tables the PDF stores as images, for example); they are listed in
            "List and review".
          </p>
        )}
        <p className="muted small">
          PDF loaded from{" "}
          <a href={pdfUrl} target="_blank" rel="noreferrer">
            arXiv:{arxivId}
          </a>
          .
        </p>
      </aside>

      <div
        className="reader-pdf"
        ref={pdfPane}
        onClick={(e) => {
          const hit = (e.target as HTMLElement).closest<HTMLElement>("[data-gap]");
          if (hit) setSelected(Number(hit.dataset.gap));
        }}
      >
        {error && (
          <p role="alert" className="error">
            Could not load the PDF from arXiv: {error}.{" "}
            <a href={pdfUrl} target="_blank" rel="noreferrer">
              Open it on arXiv
            </a>
          </p>
        )}
        {!doc && !error && <p aria-busy="true">Loading the paper from arXiv…</p>}
        {doc &&
          sizes.map((size, i) => (
            <PdfPage
              key={i + 1}
              doc={doc}
              number={i + 1}
              size={size}
              scale={scale}
              highlights={byPage.get(i + 1) ?? EMPTY}
              onRendered={onRendered}
            />
          ))}
      </div>
    </div>
  );
}

const EMPTY: Highlight[] = [];
