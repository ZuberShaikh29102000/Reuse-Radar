import { GAP_STATUSES, PRODUCT_TYPES, type GapFilters, type GapStatus, type ProductType } from "../api";
import { STATUS_LABEL, TYPE_LABEL } from "../labels";

const YEARS = ["2020", "2021", "2022", "2023", "2024", "2025"];

interface Props {
  filters: GapFilters;
  onChange: (next: GapFilters) => void;
}

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

export function Filters({ filters, onChange }: Props) {
  const update = (patch: Partial<GapFilters>) => onChange({ ...filters, ...patch, page: 1 });
  return (
    <form className="filters" aria-label="Filter gaps" onSubmit={(e) => e.preventDefault()}>
      <fieldset>
        <legend>Status</legend>
        {GAP_STATUSES.map((s: GapStatus) => (
          <label key={s} className="chip">
            <input
              type="checkbox"
              checked={filters.status.includes(s)}
              onChange={() => update({ status: toggle(filters.status, s) })}
            />
            {STATUS_LABEL[s]}
          </label>
        ))}
      </fieldset>
      <fieldset>
        <legend>Product type</legend>
        {PRODUCT_TYPES.map((t: ProductType) => (
          <label key={t} className="chip">
            <input
              type="checkbox"
              checked={filters.productType.includes(t)}
              onChange={() => update({ productType: toggle(filters.productType, t) })}
            />
            {TYPE_LABEL[t]}
          </label>
        ))}
      </fieldset>
      <div className="selects">
        <label>
          Year{" "}
          <select value={filters.year} onChange={(e) => update({ year: e.target.value })}>
            <option value="">All years</option>
            {YEARS.map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </select>
        </label>
        <label>
          Minimum severity{" "}
          <select
            value={filters.minSeverity}
            onChange={(e) => update({ minSeverity: e.target.value })}
          >
            <option value="">Any</option>
            <option value="1">Low and above</option>
            <option value="2">Medium and above</option>
            <option value="3">High only</option>
          </select>
        </label>
        <button
          type="button"
          className="link"
          onClick={() =>
            onChange({ status: [], productType: [], year: "", minSeverity: "", page: 1 })
          }
        >
          Reset filters
        </button>
      </div>
    </form>
  );
}
