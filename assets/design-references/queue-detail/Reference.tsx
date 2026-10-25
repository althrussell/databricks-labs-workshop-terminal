import { useMemo, useState } from "react";

/** Queue / detail reference. Sample data, labelled as sample; replace with the attendee's own. */
export type QueueItem = { id: string; name: string; priority: number; reason: string; ageHours: number; owner: string | null };
export type QueueExample = "default" | "long-label" | "empty" | "loading" | "error";
export const queueExamples: QueueExample[] = ["default", "long-label", "empty", "loading", "error"];

const SAMPLE: QueueItem[] = [
  { id: "c-101", name: "Sample customer A", priority: 1, reason: "Life support, off supply 3 h, no backup", ageHours: 3, owner: null },
  { id: "c-102", name: "Sample customer B", priority: 2, reason: "Life support, off supply 1 h, backup unknown", ageHours: 1, owner: null },
  { id: "c-103", name: "Sample customer C", priority: 3, reason: "Off supply 5 h, backup confirmed", ageHours: 5, owner: "Sam" },
];

function itemsFor(example: QueueExample): QueueItem[] {
  if (example === "empty" || example === "loading" || example === "error") return [];
  if (example === "long-label") {
    return SAMPLE.map((item) => ({ ...item, name: `${item.name} — Northern Regional Distribution Area, Feeder 14B` }));
  }
  return SAMPLE;
}

export function QueueDetailReference({ example = "default", onTakeOwnership }: {
  example?: QueueExample;
  /** Resolves when the owner is saved; rejects with the reason it was not. */
  onTakeOwnership?: (id: string) => Promise<void>;
}) {
  const [filter, setFilter] = useState<"all" | "unassigned">("unassigned");
  const [selected, setSelected] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);
  const [items, setItems] = useState<QueueItem[]>(() => itemsFor(example));
  const shown = useMemo(() => items.filter((item) => filter === "all" || item.owner === null), [items, filter]);
  const current = shown.find((item) => item.id === selected) ?? null;

  if (example === "loading") {
    return <div role="status" aria-label="Loading queue" className="space-y-2 p-4">
      {[0, 1, 2].map((n) => <div key={n} className="h-10 animate-pulse rounded bg-muted" />)}
    </div>;
  }
  if (example === "error") {
    return <div role="alert" className="m-4 rounded border border-destructive p-4 text-sm">
      <p className="font-medium text-destructive">The queue could not be loaded.</p>
      <p className="text-muted-foreground">The data service did not answer. Nothing was changed.</p>
      <button type="button" className="mt-2 rounded bg-primary px-3 py-1.5 text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => setItems(itemsFor("default"))}>Try again</button>
    </div>;
  }

  async function take(item: QueueItem) {
    setSaving(true);
    setOutcome(null);
    try {
      if (!onTakeOwnership) throw new Error("Saving is not connected yet, so nothing was saved.");
      await onTakeOwnership(item.id);
      setItems((all) => all.map((row) => (row.id === item.id ? { ...row, owner: "You" } : row)));
      setOutcome({ ok: true, text: `${item.name} is now yours.` });
    } catch (error) {
      setOutcome({ ok: false, text: error instanceof Error ? error.message : "Not saved." });
    } finally {
      setSaving(false);
    }
  }

  return <div className="grid gap-4 p-4 md:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
    <section aria-label="Queue" className="min-w-0">
      <div className="mb-2 flex items-center justify-between gap-2">
        <label className="text-sm">Show{" "}
          <select value={filter} onChange={(e) => setFilter(e.target.value === "all" ? "all" : "unassigned")}
            className="rounded border border-border bg-background px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <option value="unassigned">Unassigned</option><option value="all">All</option>
          </select>
        </label>
        <span className="text-sm text-muted-foreground">{shown.length} shown · sample data</span>
      </div>
      {shown.length === 0 ? <p className="rounded border border-border p-4 text-sm text-muted-foreground">
        Nobody is waiting. New interruptions appear here as soon as they are reported.
      </p> : <ul role="listbox" aria-label="Customers to call, highest priority first" className="divide-y divide-border rounded border border-border">
        {shown.map((item) => <li key={item.id} role="option" aria-selected={item.id === selected} tabIndex={0}
          onClick={() => setSelected(item.id)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") setSelected(item.id); }}
          className="grid cursor-pointer grid-cols-[2.5rem_minmax(0,1fr)] gap-x-3 px-3 py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:grid-cols-[2.5rem_minmax(0,1.2fr)_minmax(0,2fr)_4rem_5rem]">
          <span className="font-mono text-sm">#{item.priority}</span>
          <span className="truncate font-medium" title={item.name}>{item.name}</span>
          <span className="col-start-2 text-sm text-muted-foreground md:col-start-auto">{item.reason}</span>
          <span className="hidden text-sm md:block">{item.ageHours} h</span>
          <span className="hidden text-sm md:block">{item.owner ?? "—"}</span>
        </li>)}
      </ul>}
    </section>
    <aside aria-label="Selected customer" className="rounded border border-border p-4">
      {current ? <>
        <h2 className="text-base font-semibold">{current.name}</h2>
        <p className="mt-1 text-sm">Why #{current.priority}: {current.reason}.</p>
        <button type="button" disabled={saving || current.owner !== null} onClick={() => void take(current)}
          className="mt-3 rounded bg-primary px-3 py-1.5 text-primary-foreground disabled:opacity-60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          {saving ? "Saving…" : current.owner ? `Owned by ${current.owner}` : "Take ownership"}
        </button>
        {outcome ? <p role="status" className={outcome.ok ? "mt-2 text-sm text-success" : "mt-2 text-sm text-destructive"}>{outcome.text}</p> : null}
      </> : <p className="text-sm text-muted-foreground">Choose a customer to see why they are here and take ownership.</p>}
    </aside>
  </div>;
}
