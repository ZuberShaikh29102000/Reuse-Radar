// Typed client for the Reuse Radar REST API (mirrors reuse_radar/api/serializers.py).

export const PRODUCT_TYPES = [
  "cross_section",
  "upper_limit",
  "efficiency_map",
  "likelihood",
  "covariance_matrix",
  "acceptance_table",
  "cutflow",
  "correlation_matrix",
  "other",
] as const;
export type ProductType = (typeof PRODUCT_TYPES)[number];

export const GAP_STATUSES = ["missing", "no_record", "uncertain", "published"] as const;
export type GapStatus = (typeof GAP_STATUSES)[number];

export interface Paper {
  inspire_id: number;
  arxiv_id: string | null;
  title: string;
  collaboration: string;
  earliest_date: string;
  year: number;
  readiness_score: number | null;
  processing_status: string;
  hepdata_record_id: number | null;
  hepdata_version: number | null;
  inspire_url: string;
  arxiv_url: string | null;
  hepdata_url: string | null;
}

export interface LatestReview {
  verdict: "accept" | "reject";
  reviewer: string;
  created_at: string;
}

export interface Product {
  id: number;
  product_type: ProductType;
  description: string;
  evidence_span: string;
  evidence_section: string;
  evidence_kind: string;
  confidence: number;
  merged_duplicates: number;
  extraction_version: string;
  latest_review: LatestReview | null;
}

export interface PublishedTable {
  table_doi: string;
  name: string;
  description: string;
  kind: "table" | "resource";
  resource_type: string;
  doi_url: string;
}

export interface Match {
  score: number;
  embedding_similarity: number;
  caption_overlap: number;
}

export interface Gap {
  id: number;
  status: GapStatus;
  severity: number;
  product: Product;
  paper: Paper;
  matched_table: PublishedTable | null;
  match: Match | null;
}

export type PaperGap = Omit<Gap, "paper">;

export interface Page<T> {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
}

export interface Stats {
  papers: {
    total: number;
    by_processing_status: Record<string, number>;
    with_hepdata_record: number;
    mean_readiness_score: number | null;
  };
  products: { total: number; by_type: Record<string, number> };
  gaps: { open: number; by_status: Record<string, number>; by_severity: Record<string, number> };
}

export interface Review {
  id: number;
  product_id: number;
  verdict: "accept" | "reject";
  reviewer: string;
  note: string;
  created_at: string;
}

export interface GapFilters {
  status: GapStatus[];
  productType: ProductType[];
  year: string;
  minSeverity: string;
  page: number;
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// In development Vite proxies /api to the local Django server; in production VITE_API_URL is
// the Render service URL.
const BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");
export const PAGE_SIZE = 25;
export const API_DOCS_URL = `${BASE}/api/docs`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body: unknown = await response.json();
      detail = typeof body === "object" && body !== null ? JSON.stringify(body) : String(body);
    } catch {
      // keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export function gapQuery(filters: GapFilters): string {
  const params = new URLSearchParams();
  if (filters.status.length) params.set("status", filters.status.join(","));
  if (filters.productType.length) params.set("product_type", filters.productType.join(","));
  if (filters.year) params.set("year", filters.year);
  if (filters.minSeverity) params.set("min_severity", filters.minSeverity);
  params.set("page", String(filters.page));
  params.set("page_size", String(PAGE_SIZE));
  return params.toString();
}

export const api = {
  stats: () => request<Stats>("/api/stats"),
  gaps: (filters: GapFilters) => request<Page<Gap>>(`/api/gaps?${gapQuery(filters)}`),
  paper: (inspireId: number) =>
    request<{ paper: Paper; gaps: PaperGap[] }>(`/api/papers/${inspireId}/gaps`),
  review: (token: string, productId: number, verdict: "accept" | "reject", note = "") =>
    request<Review>("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ product_id: productId, verdict, note }),
    }),
};
