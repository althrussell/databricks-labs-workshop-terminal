import { useId, useState } from "react";
import { ChevronDown, FolderOpen, Plus, X } from "lucide-react";
import type { ProjectInventory, ProjectSelection, SessionInfo } from "../api";
import { buildName } from "../projects";

interface Props {
  inventory: ProjectInventory | null;
  session: SessionInfo | null;
  loading: boolean;
  loadError: string;
  onRefresh: (saved: boolean) => Promise<void>;
  onSelect: (selection: ProjectSelection, stopSessionId?: string) => Promise<string>;
}

export default function ProjectBar({ inventory, session, loading, loadError, onRefresh, onSelect }: Props) {
  const id = useId();
  const [panel, setPanel] = useState<"list" | "new" | "goal" | null>(null);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<ProjectSelection | null>(null);
  const current = inventory?.projects.find((item) => item.name === inventory.active);

  function open(mode: typeof panel) {
    setPanel(mode); setError(""); setNotice(""); setPending(null);
    setGoal(mode === "goal" ? current?.goal ?? "" : "");
    if (mode === "new") setName("");
  }

  async function apply(selection: ProjectSelection, stopSessionId?: string) {
    if (!stopSessionId && session && !session.exited &&
        (selection.new || selection.recover || selection.name !== inventory?.active)) {
      setPending(selection); setError(""); return;
    }
    setBusy(true); setError(""); setNotice("");
    try {
      const message = await onSelect(selection, stopSessionId);
      setPending(null); setPanel(null); setNotice(message);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally { setBusy(false); }
  }

  async function refresh(saved: boolean) {
    setBusy(true); setError("");
    try { await onRefresh(saved); }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  }

  return <section className="project-controls" aria-label="Workshop builds">
    <div className="project-bar">
      <span className="project-current"><FolderOpen size={16} aria-hidden="true" />
        <span>{inventory?.active ? <>Current build: <strong>{inventory.active}</strong></> : "Build something new, or return to earlier work"}</span>
      </span>
      <div className="project-actions">
        <button className="btn btn-ghost" aria-expanded={panel === "list"} aria-controls={`${id}-panel`}
          disabled={busy} onClick={() => { open(panel === "list" ? null : "list"); if (!inventory) void refresh(false); }}>
          My builds <ChevronDown size={14} aria-hidden="true" /></button>
        <button className="btn btn-ghost" disabled={busy || !inventory} onClick={() => open("new")}><Plus size={14} aria-hidden="true" /> New build</button>
        {current && !current.missing && <button className="btn btn-ghost" disabled={busy} onClick={() => open("goal")}>Change goal</button>}
      </div>
    </div>
    {panel && <div id={`${id}-panel`} className="project-panel">
      <div className="project-panel-heading"><h2>{panel === "list" ? "My builds" : panel === "new" ? "Start a separate build" : `Change ${inventory?.active}'s goal`}</h2>
        <button className="icon-btn" aria-label="Close build controls" disabled={busy} onClick={() => open(null)}><X size={16} /></button>
      </div>
      {panel === "list" ? <>
        <p>Switch between builds without mixing their files or goals. Starting a new build keeps earlier work.</p>
        {loading && <p role="status">Loading builds…</p>}
        {!loading && !inventory && <button className="btn btn-ghost" disabled={busy} onClick={() => void refresh(false)}>Retry loading builds</button>}
        {inventory?.projects.length === 0 && <p>No builds yet. You can start one here or directly in an agent.</p>}
        <ul className="project-list">{inventory?.projects.map((item) => <li key={item.name}>
          <div><strong>{item.name}{item.name === inventory.active ? " · current" : ""}</strong>
            <span>{item.goal || (item.missing ? "Local source is unavailable" : item.available ? "Ready to continue" : "Ready to start")}</span>
            {item.saved && <small>Synced source copy available; it may not include your latest edits.</small>}
          </div>
          {item.available || !item.missing ? <button className="btn btn-ghost" disabled={busy || item.name === inventory.active}
            onClick={() => void apply({ name: item.name })}>Continue</button>
            : item.saved ? <button className="btn btn-ghost" disabled={busy} onClick={() => void apply({ name: item.name, recover: true })}>Recover source</button>
              : <span className="project-source-note">Check saved builds</span>}
        </li>)}</ul>
        <button className="btn btn-ghost" disabled={busy} onClick={() => void refresh(true)}>{busy ? "Checking…" : "Check saved builds"}</button>
        <p className="project-source-note">Checks your Databricks Workspace copies. Recovery brings back source, not the original conversation or Git history. Export a ZIP before the workshop ends.</p>
      </> : <form onSubmit={(event) => {
        event.preventDefault();
        void apply({ name: panel === "new" ? buildName(name) : inventory!.active, goal: goal.trim(), new: panel === "new" });
      }}>
        {panel === "new" && <><label htmlFor={`${id}-name`}>Build name</label>
          <input id={`${id}-name`} value={name} maxLength={100} autoFocus disabled={busy}
            onChange={(event) => setName(event.target.value)} placeholder="For example, repair desk" required />
          {name && <small>Folder name: {buildName(name) || "Add a letter or number"}</small>}</>}
        <label htmlFor={`${id}-goal`}>{panel === "new" ? "What would you like to build?" : "What should change?"}</label>
        <textarea id={`${id}-goal`} value={goal} maxLength={2000} autoFocus={panel === "goal"} disabled={busy}
          onChange={(event) => setGoal(event.target.value)} placeholder="One sentence is enough to get started." rows={2} />
        <div className="project-form-actions"><button type="button" className="btn btn-ghost" disabled={busy} onClick={() => open(null)}>Cancel</button>
          <button className="btn btn-primary" disabled={busy || (panel === "new" && !buildName(name)) || (panel === "goal" && !goal.trim())}>
            {busy ? "Saving…" : panel === "new" ? "Create build" : "Save goal"}</button></div>
      </form>}
      {pending && <div className="project-switch-confirm" role="group" aria-label="Confirm build switch">
        <p>{session?.label} is running. Stop it to {pending.recover ? "recover" : "switch to"} <strong>{pending.name}</strong>? Written files stay in place; the conversation ends.</p>
        <button className="btn btn-ghost" disabled={busy} onClick={() => setPending(null)}>Keep current agent</button>
        <button className="btn btn-primary" disabled={busy} onClick={() => void apply(pending, session?.id)}>{busy ? "Switching…" : "Stop agent and switch"}</button>
      </div>}
    </div>}
    {(error || loadError) && <p className="project-feedback project-error" role="alert">{error || loadError}</p>}
    {notice && <p className="project-feedback" role="status">{notice}</p>}
    {current?.missing && panel !== "list" && <p className="project-feedback">This build's local source is missing. Open My builds and check saved builds to recover it.</p>}
  </section>;
}
