import { useCallback, useEffect, useId, useState } from "react";
import { Download, ExternalLink, MessageSquareText, Octagon, Sparkles } from "lucide-react";
import { api } from "../api";
import { availableActions, phaseLabel, reviewLabel, type BuildView } from "../build";

/**
 * "Your build" (W4): the attendee's goal, stage, next decision, preview and
 * what changed, from the structured workflow record only. The terminal stays
 * the primary workspace; this never becomes a second conversation.
 */
export default function YourBuild({ refreshKey, onGetHelp }: { refreshKey: unknown; onGetHelp: () => void }) {
  const id = useId();
  const [view, setView] = useState<BuildView | null>(null);
  const [open, setOpen] = useState(false);
  const [ask, setAsk] = useState<"change" | "explain" | null>(null);
  const [text, setText] = useState("");
  const [answer, setAnswer] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setView(await api.build()); } catch { /* unknown stays unknown; the terminal still works */ }
  }, []);
  useEffect(() => { void load(); const timer = window.setInterval(load, 15000); return () => window.clearInterval(timer); },
    [load, refreshKey]);

  async function act(run: () => Promise<string>) {
    setBusy(true); setNotice("");
    try { setNotice(await run()); await load(); }
    catch (caught) { setNotice(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  }

  if (!view || !view.project) return null;
  const actions = availableActions(view);
  return <section className="your-build" aria-labelledby={`${id}-title`}>
    <div className="your-build-bar">
      <button className="btn btn-ghost your-build-toggle" aria-expanded={open} aria-controls={`${id}-panel`}
        onClick={() => setOpen(!open)}>
        <Sparkles size={15} aria-hidden="true" />
        <span id={`${id}-title`}>Your build</span>
        <span className={`build-status build-status-${view.status}`}>{phaseLabel(view.phase)}</span>
      </button>
      <span className="your-build-next">Next: {view.next_decision}</span>
      <div className="your-build-actions">
        {actions.tryApp && <a className="btn btn-ghost" href={view.deploy.preview_url ?? undefined} target="_blank" rel="noreferrer">
          <ExternalLink size={14} aria-hidden="true" />Try the app</a>}
        {actions.stop && <button className="btn btn-ghost" disabled={busy}
          title="Sends Esc: your agent stops its current step. Work already sent to Databricks may still finish."
          onClick={() => act(async () => (await api.buildStop()).detail)}>
          <Octagon size={14} aria-hidden="true" />Stop current step</button>}
      </div>
    </div>
    {open && <div className="your-build-panel" id={`${id}-panel`}>
      {view.goal && <p><strong>Goal:</strong> {view.goal}</p>}
      <p><strong>Review:</strong> {reviewLabel(view.review)}
        {view.deploy.state === "deployed" ? " · deployed" : " · not deployed yet"}</p>
      {view.what_changed.length > 0 && <><h3>What changed</h3>
        <ul>{view.what_changed.map((line) => <li key={line}>{line}</li>)}</ul></>}
      {actions.choosePath && <div className="your-build-decision">
        <p>How do you want to work on this?</p>
        <button className="btn" disabled={busy} onClick={() => act(async () => {
          await api.buildDecide({ path: "rapid" }); return "Rapid prototype: your agent builds from what it knows."; })}>Rapid prototype</button>
        <button className="btn" disabled={busy} onClick={() => act(async () => {
          await api.buildDecide({ path: "guided" }); return "Guided ideation: your agent asks first."; })}>Guided ideation</button>
      </div>}
      {view.open_questions.map((question) => <form key={question.id} className="your-build-decision"
        onSubmit={(event) => { event.preventDefault(); void act(async () => {
          await api.buildDecide({ question_id: question.id, answer: answer[question.id] ?? "" }); return "Answer saved for your agent."; }); }}>
        <label htmlFor={`${id}-${question.id}`}>{question.text}</label>
        <input id={`${id}-${question.id}`} value={answer[question.id] ?? ""} required
          onChange={(event) => setAnswer({ ...answer, [question.id]: event.target.value })} />
        <button className="btn" disabled={busy} type="submit">Answer</button>
      </form>)}
      {actions.confirmDirection && <div className="your-build-decision">
        <p><strong>Proposed direction:</strong> {view.direction?.text}</p>
        <button className="btn" disabled={busy} onClick={() => act(async () => {
          await api.buildDecide({ confirm_direction: true }); return "Direction confirmed: your agent can build it."; })}>Build this direction</button>
      </div>}
      {view.requests.length > 0 && <p className="project-source-note">Waiting for your agent's next step: {view.requests.map((r) => r.text).join("; ")}</p>}
      {ask ? <form className="your-build-decision" onSubmit={(event) => { event.preventDefault(); void act(async () => {
        const result = await api.buildRequest(ask, text); setAsk(null); setText(""); return result.detail; }); }}>
        <label htmlFor={`${id}-ask`}>{ask === "change" ? "What should change?" : "What should your agent explain?"}</label>
        <textarea id={`${id}-ask`} value={text} required onChange={(event) => setText(event.target.value)} />
        <div className="project-form-actions">
          <button className="btn" disabled={busy} type="submit">Send to my agent</button>
          <button className="btn btn-ghost" type="button" onClick={() => setAsk(null)}>Cancel</button>
        </div>
      </form> : <div className="your-build-actions">
        <button className="btn btn-ghost" onClick={() => setAsk("change")}><MessageSquareText size={14} aria-hidden="true" />Change direction</button>
        <button className="btn btn-ghost" onClick={() => setAsk("explain")}>Explain this</button>
        <button className="btn btn-ghost" onClick={onGetHelp}>Get help</button>
        {actions.exportCode && <button className="btn btn-ghost" disabled={busy} onClick={() => act(async () => {
          const { blob, filename, volumePath } = await api.exportCode();
          const link = document.createElement("a");
          link.href = URL.createObjectURL(blob); link.download = filename; link.click();
          URL.revokeObjectURL(link.href);
          return `Downloaded ${filename}. A verified copy is also kept at ${volumePath}.`;
        })}><Download size={14} aria-hidden="true" />Export my code</button>}
      </div>}
    </div>}
    {notice && <p role="status" className="project-feedback">{notice}</p>}
  </section>;
}
