import { useEffect, useState } from "react";
import { api, PAGE_SIZE, type Gap, type GapFilters, type Page, type Stats } from "./api";
import { Filters } from "./components/Filters";
import { GapRow } from "./components/GapRow";
import { PaperPanel } from "./components/PaperPanel";
import { StatsBar } from "./components/StatsBar";
import { TokenField } from "./components/TokenField";
import { loadToken } from "./token";

const INITIAL: GapFilters = { status: [], productType: [], year: "", minSeverity: "", page: 1 };

export default function App() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [filters, setFilters] = useState<GapFilters>(INITIAL);
  const [page, setPage] = useState<Page<Gap> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState(loadToken);
  const [openPaper, setOpenPaper] = useState<number | null>(null);

  useEffect(() => {
    api.stats().then(setStats).catch(() => setStats(null));
  }, []);

  // Loading/error state is reset where the filters change (changeFilters), not in the effect.
  const changeFilters = (next: GapFilters) => {
    setLoading(true);
    setError(null);
    setFilters(next);
  };

  useEffect(() => {
    let cancelled = false;
    api
      .gaps(filters)
      .then((p) => !cancelled && setPage(p))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [filters]);

  const pages = page ? Math.max(1, Math.ceil(page.count / PAGE_SIZE)) : 1;

  return (
    <>
      <header className="top">
        <div>
          <h1>Reuse Radar</h1>
          <p className="tagline">
            Data products declared in ATLAS papers that never reached HEPData, most urgent first.
          </p>
        </div>
        <TokenField token={token} onChange={setToken} />
      </header>
      <main>
        <StatsBar stats={stats} />
        <Filters filters={filters} onChange={changeFilters} />
        <section aria-labelledby="queue-title">
          <h2 id="queue-title">
            Triage queue{" "}
            {page && <span className="muted">({page.count.toLocaleString()} matching)</span>}
          </h2>
          {error && (
            <p role="alert" className="error">
              Could not load gaps: {error}
            </p>
          )}
          {loading && !page && <p aria-busy="true">Loading…</p>}
          {page && page.results.length === 0 && <p>No gaps match these filters.</p>}
          {page && (
            <ul className="gaps" aria-busy={loading}>
              {page.results.map((g) => (
                <GapRow key={g.id} gap={g} token={token} onOpenPaper={setOpenPaper} />
              ))}
            </ul>
          )}
          {page && pages > 1 && (
            <nav className="pager" aria-label="Pages">
              <button
                type="button"
                disabled={filters.page <= 1}
                onClick={() => changeFilters({ ...filters, page: filters.page - 1 })}
              >
                Previous
              </button>
              <span>
                Page {filters.page} of {pages}
              </span>
              <button
                type="button"
                disabled={filters.page >= pages}
                onClick={() => changeFilters({ ...filters, page: filters.page + 1 })}
              >
                Next
              </button>
            </nav>
          )}
        </section>
      </main>
      <footer className="muted">
        Every product carries a verbatim evidence quote from the paper. Statuses come from
        comparing the paper with HEPData's record; "Needs review" items are for a curator to
        decide. Data: INSPIRE-HEP, arXiv, HEPData via DataCite.
      </footer>
      {openPaper !== null && (
        <PaperPanel inspireId={openPaper} token={token} onClose={() => setOpenPaper(null)} />
      )}
    </>
  );
}
