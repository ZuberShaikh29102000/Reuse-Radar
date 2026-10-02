import type { Stats } from "../api";
import { pct } from "../labels";

export function StatsBar({ stats }: { stats: Stats | null }) {
  if (!stats) return <div className="stats" aria-busy="true" />;
  const reconciled = stats.papers.by_processing_status["reconciled"] ?? 0;
  const items = [
    { label: "Papers in corpus", value: stats.papers.total.toLocaleString() },
    { label: "Papers analysed", value: reconciled.toLocaleString() },
    { label: "Data products found", value: stats.products.total.toLocaleString() },
    { label: "Open gaps", value: stats.gaps.open.toLocaleString(), accent: true },
    { label: "Mean readiness", value: `${pct(stats.papers.mean_readiness_score)} / 100` },
  ];
  return (
    <dl className="stats">
      {items.map((item) => (
        <div key={item.label} className={item.accent ? "stat accent" : "stat"}>
          <dt>{item.label}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
