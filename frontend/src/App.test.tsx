import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { gapQuery, type Gap, type Stats } from "./api";

const stats: Stats = {
  papers: {
    total: 624,
    by_processing_status: { reconciled: 24, harvested: 600 },
    with_hepdata_record: 20,
    mean_readiness_score: 61.2,
  },
  products: { total: 185, by_type: { likelihood: 6 } },
  gaps: { open: 108, by_status: { missing: 43 }, by_severity: { "3": 10 } },
};

const gap: Gap = {
  id: 1,
  status: "missing",
  severity: 3,
  product: {
    id: 42,
    product_type: "likelihood",
    description: "Two-dimensional likelihood scan of ttW+ and ttW-",
    evidence_span: "Two-dimensional likelihood scan of the $\\ttW^{+}$ and $\\ttW^{-}$ cross-sections.",
    evidence_section: "Results > Inclusive cross-section",
    evidence_kind: "caption",
    confidence: 0.9,
    merged_duplicates: 0,
    extraction_version: "extract_v1+filter_1",
    latest_review: null,
  },
  paper: {
    inspire_id: 2745375,
    arxiv_id: "2401.05299",
    title: "Measurement of ttW cross-sections",
    collaboration: "ATLAS",
    earliest_date: "2024-01-10",
    year: 2024,
    readiness_score: 17.4,
    processing_status: "reconciled",
    hepdata_record_id: 149762,
    hepdata_version: 1,
    inspire_url: "https://inspirehep.net/literature/2745375",
    arxiv_url: "https://arxiv.org/abs/2401.05299",
    hepdata_url: "https://www.hepdata.net/record/ins2745375",
  },
  matched_table: null,
  match: { score: 0.32, embedding_similarity: 0.31, caption_overlap: 0.12 },
};

type Handler = (url: string, init?: RequestInit) => Response;

function mockFetch(extra?: Handler) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const custom = extra?.(url, init);
    if (custom) return custom;
    if (url.startsWith("/api/stats")) return Response.json(stats);
    if (url.startsWith("/api/gaps")) {
      return Response.json({ count: 1, next: null, previous: null, results: [gap] });
    }
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

// jsdom cannot render PDFs; the reader is exercised end to end in a real browser instead.
vi.mock("./components/PaperReader", () => ({
  PaperReader: ({ arxivId }: { arxivId: string }) => <div>PDF reader for {arxivId}</div>,
}));

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});
afterEach(() => vi.unstubAllGlobals());

describe("introduction", () => {
  it("explains the project, links the code and states independence", async () => {
    mockFetch();
    render(<App />);
    const intro = screen.getByRole("region", { name: /never made reusable/ });
    expect(within(intro).getByRole("link", { name: "Source code" })).toHaveAttribute(
      "href",
      "https://github.com/ZuberShaikh29102000/Reuse-Radar",
    );
    expect(within(intro).getByRole("link", { name: "API documentation" })).toHaveAttribute("href", "/api/docs");
    expect(within(intro).getByText(/not affiliated with or endorsed by CERN/)).toBeInTheDocument();
    await screen.findByText(/likelihood scan of ttW/);
  });

  it("can be hidden, stays hidden on reload, and can be reopened", async () => {
    mockFetch();
    const user = userEvent.setup();
    const { unmount } = render(<App />);
    await user.click(screen.getByRole("button", { name: "Hide introduction" }));
    expect(screen.queryByRole("region", { name: /never made reusable/ })).not.toBeInTheDocument();
    unmount();

    render(<App />);
    expect(screen.queryByRole("region", { name: /never made reusable/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "About this project" }));
    expect(screen.getByRole("region", { name: /never made reusable/ })).toBeInTheDocument();
    await screen.findByText(/likelihood scan of ttW/);
  });
});

describe("triage queue", () => {
  it("shows corpus stats and the gaps", async () => {
    mockFetch();
    render(<App />);
    expect(await screen.findByText("Two-dimensional likelihood scan of ttW+ and ttW-")).toBeInTheDocument();
    expect(screen.getByText("108")).toBeInTheDocument();
    const queue = within(screen.getByRole("region", { name: /Triage queue/ })).getByRole("list");
    expect(within(queue).getByText("Missing from HEPData")).toBeInTheDocument();
    expect(within(queue).getByText("High")).toBeInTheDocument();
  });

  it("expands a gap to show the verbatim evidence and links", async () => {
    mockFetch();
    const user = userEvent.setup();
    render(<App />);
    const toggle = await screen.findByRole("button", { name: /likelihood scan of ttW/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/\\ttW\^\{\+\}/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "HEPData record" })).toHaveAttribute(
      "href",
      "https://www.hepdata.net/record/ins2745375",
    );
    expect(screen.getByText(/Add your curator token/)).toBeInTheDocument();
  });

  it("sends filters to the API", async () => {
    const calls = mockFetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText(/likelihood scan of ttW/);
    await user.click(screen.getByRole("checkbox", { name: "Likelihood" }));
    await user.selectOptions(screen.getByRole("combobox", { name: /Year/ }), "2024");
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("product_type=likelihood") && c.url.includes("year=2024"))).toBe(true),
    );
  });
});

describe("paper panel", () => {
  it("shows the paper side by side with its HEPData record, and a list view", async () => {
    const table = (n: number, name: string) => ({
      table_doi: `10.17182/hepdata.149762.v1/t${n}`,
      name,
      description: `${name} description`,
      kind: "table" as const,
      resource_type: "",
      doi_url: `https://doi.org/10.17182/hepdata.149762.v1/t${n}`,
    });
    const limits = table(1, "Observed limits");
    const { paper, ...missing } = gap;
    const published = {
      ...missing,
      id: 2,
      status: "published" as const,
      severity: 0,
      product: { ...gap.product, id: 43, description: "Upper limits on ttW" },
      matched_table: limits,
    };
    mockFetch((url) =>
      url.startsWith("/api/papers/2745375/gaps")
        ? Response.json({ paper, gaps: [missing, published], hepdata_tables: [limits, table(2, "Cut-flow")] })
        : undefined!,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Measurement of ttW cross-sections" }));

    // The original paper with highlights is the default view when the paper is on arXiv.
    expect(await screen.findByText("PDF reader for 2401.05299")).toBeInTheDocument();
    await user.click(screen.getByText("Side by side with HEPData"));

    const comparison = (await screen.findByText("1 matched")).closest(".comparison") as HTMLElement;
    const view = within(comparison);
    expect(view.getByText("1 missing from HEPData")).toBeInTheDocument();
    expect(view.getByText("1 only on HEPData")).toBeInTheDocument();
    expect(view.getByText("Observed limits").closest("li")).toHaveTextContent("Upper limits on ttW");
    expect(view.getByText("Cut-flow").closest("li")).toHaveTextContent("Nothing in the paper matched this");

    await user.click(screen.getByText("List and review"));
    expect(screen.queryByText("1 matched")).not.toBeInTheDocument();
  });

  it("falls back to the list when the API does not send the HEPData tables yet", async () => {
    const { paper, ...missing } = gap;
    mockFetch((url) =>
      url.startsWith("/api/papers/2745375/gaps") ? Response.json({ paper, gaps: [missing] }) : undefined!,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "Measurement of ttW cross-sections" }));
    expect(await screen.findByText(/1 declared data products/)).toBeInTheDocument();
    expect(screen.queryByText("Side by side with HEPData")).not.toBeInTheDocument();
    expect(screen.queryByText(/missing from HEPData$/)).not.toBeInTheDocument();
  });
});

describe("curator review", () => {
  it("posts a verdict with the token and shows it", async () => {
    const calls = mockFetch((url, init) =>
      url === "/api/reviews" && init?.method === "POST"
        ? Response.json(
            { id: 7, product_id: 42, verdict: "accept", reviewer: "alice", note: "", created_at: "2026-10-02T12:00:00Z" },
            { status: 201 },
          )
        : undefined!,
    );
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByLabelText(/Curator token/), "alice-token-0123456789abcdef");
    await user.click(screen.getByRole("button", { name: "Use" }));
    await user.click(await screen.findByRole("button", { name: /likelihood scan of ttW/ }));
    await user.click(screen.getByRole("button", { name: "Accept" }));

    expect(await screen.findByText("Accepted by alice")).toBeInTheDocument();
    const post = calls.find((c) => c.url === "/api/reviews");
    expect(post?.init?.headers).toMatchObject({ Authorization: "Bearer alice-token-0123456789abcdef" });
    expect(JSON.parse(String(post?.init?.body))).toEqual({ product_id: 42, verdict: "accept", note: "" });
  });

  it("explains a rejected token", async () => {
    mockFetch((url) => (url === "/api/reviews" ? new Response("{}", { status: 401 }) : undefined!));
    sessionStorage.setItem("reuse-radar-curator-token", "wrong-token-0123456789abcdef");
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: /likelihood scan of ttW/ }));
    await user.click(screen.getByRole("button", { name: "Reject" }));
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/token was rejected/)).toBeInTheDocument();
  });
});

describe("gapQuery", () => {
  it("encodes only the filters that are set", () => {
    expect(gapQuery({ status: [], productType: [], year: "", minSeverity: "", page: 1 })).toBe(
      "page=1&page_size=25",
    );
    expect(
      gapQuery({ status: ["missing", "no_record"], productType: ["likelihood"], year: "2024", minSeverity: "2", page: 3 }),
    ).toBe("status=missing%2Cno_record&product_type=likelihood&year=2024&min_severity=2&page=3&page_size=25");
  });
});

describe("latexToText", () => {
  it("renders common title markup and leaves plain text alone", async () => {
    const { latexToText } = await import("./latex");
    expect(
      latexToText(String.raw`cross-sections of $ t\overline{t}W $ production at $ \sqrt{s} $ = 13 TeV`),
    ).toBe("cross-sections of tt̄W production at √s = 13 TeV");
    // Subscript letters have no Unicode form, so they keep a visible underscore.
    expect(latexToText(String.raw`phase $\phi_s$ in $B^0_s \to J/\psi\phi$ decays`)).toBe(
      "phase φ_s in B⁰_s → J/ψφ decays",
    );
    expect(latexToText("Search for dijet resonances")).toBe("Search for dijet resonances");
  });
});
