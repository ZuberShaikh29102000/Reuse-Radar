import type { GapStatus, ProductType } from "./api";

export const STATUS_LABEL: Record<GapStatus, string> = {
  missing: "Missing from HEPData",
  no_record: "No HEPData record",
  uncertain: "Needs review",
  published: "On HEPData",
};

export const STATUS_HELP: Record<GapStatus, string> = {
  missing: "The paper has a HEPData record, but this product was not found in it.",
  no_record: "The paper has no HEPData record at all.",
  uncertain: "A HEPData table might match. A curator should decide.",
  published: "Found on HEPData.",
};

export const TYPE_LABEL: Record<ProductType, string> = {
  cross_section: "Cross-section",
  upper_limit: "Upper limit",
  efficiency_map: "Efficiency map",
  likelihood: "Likelihood",
  covariance_matrix: "Covariance matrix",
  acceptance_table: "Acceptance table",
  cutflow: "Cut-flow",
  correlation_matrix: "Correlation matrix",
  other: "Other result",
};

export const SEVERITY_LABEL: Record<number, string> = {
  0: "Not a gap",
  1: "Low",
  2: "Medium",
  3: "High",
};

export function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value)}`;
}
