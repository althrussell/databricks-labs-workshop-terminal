import { useMemo, useState } from "react";

/** Analytical exploration reference. Sample data, labelled as sample; replace with the attendee's own. */
export type Row = { region: string; month: string; interruptions: number; customers: number };
export type AnalysisExample = "default" | "long-label" | "empty" | "loading" | "error";
export const analysisExamples: AnalysisExample[] = ["default", "long-label", "empty", "loading", "error"];

const SAMPLE: Row[] = [
  { region: "North", month: "2026-07", interruptions: 42, customers: 18000 },
  { region: "North", month: "2026-08", interruptions: 55, customers: 18100 },
  { region: "South", month: "2026-07", interruptions: 12, customers: 2400 },
  { region: "South", month: "2026-08", interruptions: 9, customers: 2410 },
];
const SOURCE = "Sample table `outages_monthly` (illustrative values)";
const SMALL_GROUP = 5000;

export function AnalyticalExplorationReference({ example = "default" }: { example?: AnalysisExample }) {
  const [region, setRegion] = useState<string>("all");
  const rows = useMemo<Row[]>(() => {
    if (example === "empty") return [];
    const base = example === "long-label"
      ? SAMPLE.map((row) => ({ ...row, region: `${row.region} Coastal and Hinterland Distribution Region` }))
      : SAMPLE;
    return base.filter((row) => region === "all" || row.region.startsWith(region));
  }, [example, region]);

  if (example === "loading") return <div role="status" aria-label="Loading analysis" className="h-48 animate-pulse rounded bg-muted m-4" />;
  if (example === "error") return <div role="alert" className="m-4 rounded border border-destructive p-4 text-sm">
    <p className="font-medium text-destructive">The analysis could not run.</p>
    <p className="text-muted-foreground">The query returned an error. The numbers below are not shown until it succeeds.</p>
  </div>;

  const total = rows.reduce((sum, row) => sum + row.interruptions, 0);
  const customers = rows.reduce((sum, row) => sum + row.customers, 0);
  const rate = customers ? (total / customers) * 1000 : null;
  const max = Math.max(1, ...rows.map((row) => row.interruptions));

  return <div className="space-y-4 p-4">
    <div className="flex flex-wrap items-center gap-3">
      <label className="text-sm">Region{" "}
        <select value={region} onChange={(e) => setRegion(e.target.value)}
          className="rounded border border-border bg-background px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          <option value="all">All regions</option><option value="North">North</option><option value="South">South</option>
        </select>
      </label>
      <span className="text-sm text-muted-foreground">Jul–Aug 2026 · {SOURCE}</span>
    </div>
    {rows.length === 0 ? <p className="rounded border border-border p-4 text-sm text-muted-foreground">
      No interruptions for this filter. Choose all regions or a wider period.
    </p> : <>
      <p className="text-sm"><span className="font-semibold">{total}</span> interruptions,{" "}
        {rate === null ? "rate unavailable" : <><span className="font-semibold">{rate.toFixed(1)}</span> per 1,000 customers</>} in the period shown.
      </p>
      <figure aria-label="Interruptions by region and month (the table below has the same values)" className="space-y-1">
        {rows.map((row) => <div key={`${row.region}-${row.month}`} className="grid grid-cols-[minmax(0,9rem)_1fr_3rem] items-center gap-2 text-sm">
          <span className="truncate" title={`${row.region} ${row.month}`}>{row.region} · {row.month}</span>
          <span className="h-3 rounded bg-primary" style={{ width: `${(row.interruptions / max) * 100}%` }} />
          <span className="text-right font-mono">{row.interruptions}</span>
        </div>)}
      </figure>
      <table className="hidden w-full text-sm md:table">
        <thead><tr className="text-left text-muted-foreground"><th>Region</th><th>Month</th><th>Interruptions</th><th>Per 1,000 customers</th></tr></thead>
        <tbody>{rows.map((row) => <tr key={`t-${row.region}-${row.month}`} className="border-t border-border">
          <td className="py-1">{row.region}</td><td>{row.month}</td><td>{row.interruptions}</td>
          <td>{((row.interruptions / row.customers) * 1000).toFixed(1)}{row.customers < SMALL_GROUP ? " (small group: treat with care)" : ""}</td>
        </tr>)}</tbody>
      </table>
      <dl className="space-y-2 text-sm md:hidden">{rows.map((row) => <div key={`m-${row.region}-${row.month}`} className="rounded border border-border p-2">
        <dt className="font-medium">{row.region} · {row.month}</dt>
        <dd>{row.interruptions} interruptions · {((row.interruptions / row.customers) * 1000).toFixed(1)} per 1,000{row.customers < SMALL_GROUP ? " (small group)" : ""}</dd>
      </div>)}</dl>
      <p className="text-xs text-muted-foreground">A screening view: it shows where to look, it is not an engineering determination.</p>
    </>}
  </div>;
}
