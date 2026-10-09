import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ChevronDown, Database, Loader2, ShieldCheck, Sparkles } from "lucide-react";
import { api, ApiError, AgentInfo, WizardBrief, WizardIdea, WizardState } from "../api";
import AgentCards, { SetupProgress, SetupSteps, SETUP_POLL_MS } from "./AgentCards";
import { humanIndustry } from "../wizard";
import { WizardRequests } from "../wizardRequests";
import HelpPreference from "./HelpPreference";
import { trapDialogTab } from "../dialog";

interface Props {
  agents: AgentInfo[];
  launching: string | null;
  onLaunch: (agentId: string, starterPrompt: string) => Promise<void>;
  onOpenAgent?: () => void;
  onClose: () => void;
  draftKey?: string;
  onSaved?: (brief: WizardBrief) => void;
}

interface Draft {
  version: 1;
  revision: number;
  savedAt: number;
  what: string;
  industry: string;
  industryStated: boolean;
  intent: string;
  stack: string[];
  selected: WizardIdea | null;
  newProject?: boolean;
}

/** A goal or a recommendation, then a ready agent. Industry stays optional. */
export default function Wizard({ agents, launching, onLaunch, onOpenAgent, onClose, draftKey, onSaved }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const coordinator = useRef(new WizardRequests());
  const [state, setState] = useState<WizardState | null>(null);
  const [step, setStep] = useState(1);
  const [what, setWhat] = useState("");
  const [industry, setIndustry] = useState("");
  const [industryStated, setIndustryStated] = useState(false);
  const [intent, setIntent] = useState("");
  const [stack, setStack] = useState<string[]>([]);
  const [displayName, setDisplayName] = useState("");
  const [selected, setSelected] = useState<WizardIdea | null>(null);
  const [newProject, setNewProject] = useState(false);
  const [ideas, setIdeas] = useState<WizardIdea[]>([]);
  const [showIdeas, setShowIdeas] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [other, setOther] = useState("");
  const [otherOpen, setOtherOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [launchBusy, setLaunchBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [ideaNotice, setIdeaNotice] = useState("");
  const [draftNotice, setDraftNotice] = useState("");
  const [starter, setStarter] = useState("");
  const [conflict, setConflict] = useState<WizardBrief | null>(null);
  const [steps, setSteps] = useState<SetupSteps | null>(null);
  const [installing, setInstalling] = useState(false);

  function applyBrief(brief: WizardBrief, defaultIndustry = "") {
    setWhat(brief.what_building);
    setIndustry(brief.industry || ((brief.revision ?? 0) === 0 && !brief.seen ? defaultIndustry : ""));
    setIndustryStated(Boolean(brief.industry_stated));
    setIntent(brief.intent);
    setStack(brief.current_stack);
    setSelected(brief.selected_idea ? { ...brief.selected_idea, selection_token: brief.selection_token } : null);
  }

  async function load() {
    const request = coordinator.current.begin();
    setError("");
    try {
      const loaded = await api.wizard(undefined, undefined, request.signal);
      if (!coordinator.current.current(request)) return;
      setState(loaded);
      applyBrief(loaded.brief, loaded.default_industry);
      setIdeas(loaded.ideas);
      if (draftKey) {
        try {
          const raw = localStorage.getItem(draftKey);
          const draft: Draft | null = raw ? JSON.parse(raw) : null;
          if (draft?.version === 1 && typeof draft.what === "string" && typeof draft.industry === "string"
              && typeof draft.intent === "string" && typeof draft.industryStated === "boolean"
              && Array.isArray(draft.stack) && draft.stack.every((item) => typeof item === "string")
              && typeof draft.savedAt === "number" && draft.savedAt <= Date.now()
              && Date.now() - draft.savedAt < 24 * 60 * 60 * 1000) {
            if (draft.revision === (loaded.brief.revision ?? 0)) {
              setWhat(draft.what); setIndustry(draft.industry); setIndustryStated(draft.industryStated);
              setIntent(draft.intent); setStack(draft.stack);
              setSelected(validSelection(draft.selected) ? draft.selected : null);
              setNewProject(Boolean(draft.newProject));
              setDraftNotice("Your unfinished draft is restored.");
            } else {
              setDraftNotice("The saved goal changed since your browser draft. The current saved goal is shown.");
            }
          }
        } catch {
          setDraftNotice("This browser could not restore a draft. Your saved goal is shown.");
        }
      }
    } catch (caught) {
      if (coordinator.current.current(request)) setError(message(caught));
    }
  }

  useEffect(() => {
    const element = dialog.current;
    const returnFocus = document.activeElement as HTMLElement | null;
    const priorOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    element?.showModal();
    void load();
    return () => {
      coordinator.current.cancel();
      element?.close();
      document.body.style.overflow = priorOverflow;
      if (returnFocus?.isConnected) returnFocus.focus();
    };
    // Load once: setup polling and parent callbacks must never replace edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!state || !draftKey || step !== 1) return;
    const draft: Draft = { version: 1, revision: state.brief.revision ?? 0, savedAt: Date.now(),
      what, industry, industryStated, intent, stack, selected, newProject };
    try { localStorage.setItem(draftKey, JSON.stringify(draft)); }
    catch { setDraftNotice("This browser cannot keep an unfinished draft. Continue to save your goal."); }
  }, [state, draftKey, step, what, industry, industryStated, intent, stack, selected, newProject]);

  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const status = await api.setupStatus();
        if (active) { setSteps(status.steps); setInstalling(status.installing); }
      } catch { /* Readiness comes from the real cards; a failed poll adds no claim. */ }
    };
    void poll();
    const timer = setInterval(poll, SETUP_POLL_MS);
    return () => { active = false; clearInterval(timer); };
  }, []);

  function cancelCandidates(clear = false) {
    coordinator.current.cancel();
    setBusy(false);
    if (clear) { setIdeas([]); setSelected(null); setIdeaNotice("Your context changed. Show or suggest ideas again."); }
  }

  async function suggestions(generated: boolean) {
    const request = coordinator.current.begin();
    setBusy(true); setShowIdeas(true); setIdeaNotice("");
    try {
      if (generated) {
        const result = await api.wizardSuggest({ text: what, industry, industry_locked: industryStated, intent }, request.signal);
        if (!coordinator.current.current(request)) return;
        setIdeas(result.ideas);
        setIdeaNotice(result.source === "selector" ? (result.fallback_reason ?? "Generated ideas were unavailable. Continue with your goal, or choose a matching curated idea.") : "Ideas suggested for your goal. Choose one or keep your own words.");
      } else {
        const result = await api.wizard(industry, what, request.signal);
        if (!coordinator.current.current(request)) return;
        setIdeas(result.ideas);
        setIdeaNotice("Curated workshop ideas. Your agent will check fit and access before building.");
      }
    } catch (caught) {
      if (coordinator.current.current(request)) setIdeaNotice("Ideas could not load. Try again, or continue with your own goal. " + message(caught));
    } finally {
      if (coordinator.current.current(request)) setBusy(false);
    }
  }

  function pick(idea: WizardIdea) {
    cancelCandidates();
    setSelected({ ...idea });
    setShowIdeas(false);
    setError("");
    // The recommendation is separate from the attendee's words and industry.
  }

  async function skip() {
    if (saving || launchBusy) return;
    cancelCandidates(); setSaving(true); setError("");
    try {
      await api.saveWizard({ operation: "skip", expected_revision: state?.brief.revision ?? 0 });
      if (draftKey) localStorage.removeItem(draftKey);
      onClose();
    } catch (caught) { setError("Your dismissal could not be saved. " + message(caught)); }
    finally { setSaving(false); }
  }

  async function continueToAgent(revision = state?.brief.revision ?? 0) {
    cancelCandidates(); setSaving(true); setError("");
    try {
      const saved = await api.saveWizard({ operation: newProject ? "change" : "complete", expected_revision: revision,
        what_building: what, industry, industry_stated: industryStated,
        intent, current_stack: stack, idea_id: selected?.id ?? "",
        selection_token: selected?.selection_token ?? "",
        ...(displayName.trim() ? { display_name: displayName.trim() } : {}) });
      if (!saved.starter_prompt) throw new Error("Add a goal or choose an idea before opening an agent.");
      setState((current) => current ? { ...current, brief: saved.brief } : current);
      onSaved?.(saved.brief);
      setStarter(saved.starter_prompt); setConflict(null); setStep(2);
      setNewProject(false);
      if (draftKey) localStorage.removeItem(draftKey);
    } catch (caught) {
      setError(message(caught));
      if (caught instanceof ApiError && caught.status === 409 && caught.detail && typeof caught.detail === "object" && "brief" in caught.detail) {
        setConflict(caught.detail.brief as WizardBrief);
      }
    } finally { setSaving(false); }
  }

  async function launch(id: string) {
    cancelCandidates(); setLaunchBusy(id); setError("");
    try { await onLaunch(id, starter); }
    catch (caught) { setError("Your goal is saved. " + message(caught)); }
    finally { setLaunchBusy(null); }
  }

  async function clearGoal() {
    if (!state) return;
    cancelCandidates(); setSaving(true); setError("");
    try {
      const saved = await api.saveWizard({ operation: "clear", expected_revision: state.brief.revision ?? 0 });
      onSaved?.(saved.brief);
      if (draftKey) localStorage.removeItem(draftKey);
      onClose();
    } catch (caught) { setError(message(caught)); }
    finally { setSaving(false); }
  }

  const canContinue = Boolean(what.trim() || selected);
  const labelIndustry = (value: string) => state?.industry_labels?.[value] ?? humanIndustry(value);
  const pending = saving || Boolean(launchBusy || launching);

  return (
    <dialog ref={dialog} className="modal modal-wide wizard" aria-labelledby="wizard-title" aria-describedby="wizard-description" onKeyDown={trapDialogTab}
      onCancel={(event) => { event.preventDefault(); if (!pending) void skip(); }}>
      <div className="wizard-head">
        <span className="wizard-step-count">Step {step} of 2</span>
        <button className="wizard-skip" disabled={pending} onClick={() => void skip()}>Skip onboarding</button>
      </div>
      <div className="wizard-body">
        <h2 id="wizard-title" className="wizard-title">{step === 1 ? "What would you like to build?" : "Your goal is saved"}</h2>
        <p id="wizard-description" className="wizard-sub">{step === 1
          ? "Tell us the useful thing you have in mind, or choose an idea. Your agent will help shape a small first version."
          : "Choose a ready agent. It opens with your goal in the prompt, ready for you to send."}</p>
        {error && <div className="wizard-error" role="alert"><p>{error}</p>
          {error.includes("delivery was interrupted") && onOpenAgent && <button className="btn btn-ghost" onClick={onOpenAgent}>Open agent</button>}
          {conflict && <><p>Saved in another tab: {conflict.what_building || conflict.selected_idea?.label}</p>
            <button className="btn btn-ghost" disabled={saving} onClick={() => { applyBrief(conflict); setState((current) => current ? { ...current, brief: conflict } : current); setConflict(null); setError(""); }}>Load saved goal</button>
            <button className="btn btn-ghost" disabled={saving} onClick={() => void continueToAgent(conflict.revision ?? 0)}>Save my draft instead</button></>}
          {!state && <button className="btn btn-ghost" onClick={() => void load()}>Retry loading</button>}
        </div>}
        {!state && !error && <p role="status"><Loader2 size={16} className="spin" /> Loading your saved goal…</p>}
        {state && step === 1 && <fieldset className="wizard-fields" disabled={pending} aria-label="Goal and context">
          <label className="wizard-field-label" htmlFor="wizard-goal">Your goal</label>
          <textarea id="wizard-goal" className="wizard-input" rows={3} autoFocus maxLength={2000}
            placeholder="For example, help my bakery staff keep track of today's orders" value={what}
            onChange={(event) => { cancelCandidates(true); setWhat(event.target.value); setError(""); }} />
          {draftNotice && <p className="wizard-industry-note" role="status">{draftNotice}</p>}
          {selected && <div className="wizard-selection">
            <span className="wizard-field-label">Selected idea</span><strong>{selected.label}</strong><p>{selected.outcome}</p>
            {selected.fit_reason && <p>{selected.fit_reason}</p>}
            {selected.first_version && <p>First version: {selected.first_version}</p>}
            {selected.assumptions?.map((assumption) => <p key={assumption}>Demo assumption: {assumption}</p>)}
            {selected.unresolved?.[0] && <p>Your agent can help settle: {selected.unresolved[0]}</p>}
            <button className="wizard-ghost" onClick={() => { cancelCandidates(); setSelected(null); }}>Remove idea and keep my words</button>
          </div>}
          <div className="wizard-actions-inline">
            <button className="wizard-ghost" onClick={() => void suggestions(false)}><Sparkles size={14} /> Help me choose</button>
            {state.llm_wizard?.enabled && <button className="wizard-ghost" disabled={!what.trim()} onClick={() => void suggestions(true)}>Suggest ideas for my goal</button>}
            {showIdeas && <button className="wizard-ghost" onClick={() => { cancelCandidates(); setShowIdeas(false); }}>Hide ideas</button>}
          </div>
          {showIdeas && <div className="wizard-ideas" aria-busy={busy}>
            {busy && <p role="status"><Loader2 size={14} className="spin" /> Finding ideas… You can keep typing or continue.</p>}
            {ideaNotice && <p className="wizard-industry-note" role="status">{ideaNotice}</p>}
            {!busy && ideas.length === 0 && <p>No matching ideas yet. Try another description, or continue with your own goal.</p>}
            <div className="wizard-idea-grid">{ideas.map((idea) => <button key={idea.id} className="wizard-idea" onClick={() => pick(idea)}>
              <span className="wizard-idea-label">{idea.label}</span><span className="wizard-idea-outcome">{idea.outcome}</span>
              {idea.fit_reason && <span className="wizard-idea-outcome">{idea.fit_reason}</span>}
              {idea.data_ready && <span className="wizard-idea-badge"><Database size={10} /> Prepared tables available</span>}
              {idea.data_mode === "demo" && <span className="wizard-idea-generic">Workshop demo data; your agent will check its source, dates and access</span>}
              {idea.data_mode === "generate" && <span className="wizard-idea-generic">Data to confirm; explore existing data before choosing samples</span>}
            </button>)}</div>
          </div>}
          <button className="wizard-context-toggle" aria-expanded={contextOpen} aria-controls="wizard-context" onClick={() => setContextOpen(!contextOpen)}>
            <ChevronDown size={14} className={contextOpen ? "wizard-chevron-open" : ""} /> Optional context</button>
          {contextOpen && <div id="wizard-context" className="wizard-context">
            <HelpPreference />
            {state.brief.stage === "complete" && <div className="wizard-actions-inline">
              <button className="btn btn-ghost" disabled={pending} onClick={() => { cancelCandidates(true); setWhat(""); setNewProject(true); setDraftNotice("Your previous task stays saved until you continue with a new project."); }}>Start a different project</button>
              <button className="btn btn-ghost" disabled={pending} onClick={() => void clearGoal()}>Clear saved goal</button>
            </div>}
            <div className="wizard-field"><label className="wizard-field-label" htmlFor="wizard-name">Your name (optional)</label>
              <input id="wizard-name" className="wizard-other-input" value={displayName} maxLength={60} onChange={(event) => setDisplayName(event.target.value)} placeholder="So your host knows who's here" /></div>
            <div className="wizard-field"><span className="wizard-field-label">Industry (optional)</span>
              <div className="wizard-chips">{state.industries.map((value) => <button key={value} className={`hero-chip ${industry === value && industryStated ? "hero-chip-active" : ""}`} aria-pressed={industry === value && industryStated}
                onClick={() => { cancelCandidates(true); setIndustry(industry === value && industryStated ? "" : value); setIndustryStated(!(industry === value && industryStated)); setOtherOpen(false); }}>{labelIndustry(value)}</button>)}
                <button className="hero-chip" aria-expanded={otherOpen} onClick={() => setOtherOpen(!otherOpen)}>Other industry</button>
                {industry && <button className="hero-chip" onClick={() => { cancelCandidates(true); setIndustry(""); setIndustryStated(false); }}>Clear industry</button>}
              </div>
              {otherOpen && <div className="wizard-other-row"><label htmlFor="wizard-other">Your industry</label>
                <input id="wizard-other" className="wizard-other-input" value={other} maxLength={64} onChange={(event) => setOther(event.target.value)} />
                <button className="btn btn-ghost" onClick={() => { cancelCandidates(true); setIndustry(other.trim()); setIndustryStated(Boolean(other.trim())); setOtherOpen(false); }}>Use this industry</button></div>}
              {industry && !industryStated && <p className="wizard-industry-note">The room suggests {labelIndustry(industry)}. It stays a suggestion until you choose an industry.</p>}
            </div>
            <div className="wizard-field"><span className="wizard-field-label">Why are you building it?</span><div className="wizard-chips">{state.intents.map((value) => <button key={value} aria-pressed={intent === value} className={`hero-chip ${intent === value ? "hero-chip-active" : ""}`}
              onClick={() => { cancelCandidates(true); setIntent(intent === value ? "" : value); }}>{state.intent_labels?.[value] ?? value}</button>)}</div></div>
            <div className="wizard-field"><span className="wizard-field-label">Tools you use today (optional)</span><div className="wizard-chips">{state.stacks?.map((value) => <button key={value} aria-pressed={stack.includes(value)} className={`hero-chip ${stack.includes(value) ? "hero-chip-active" : ""}`}
              onClick={() => { cancelCandidates(true); setStack(stack.includes(value) ? stack.filter((item) => item !== value) : [...stack, value]); }}>{value}</button>)}</div></div>
          </div>}
          {state.capture_enabled && <p className="wizard-notice"><ShieldCheck size={14} /> When you continue, your host can see this completed goal for follow-up. You can view or withdraw it in Insights.</p>}
          <div className="wizard-foot"><span>Your agent can fill in the gaps.</span><button className="btn btn-primary" disabled={!canContinue || pending} onClick={() => void continueToAgent()}>{saving && <Loader2 size={14} className="spin" />} Continue</button></div>
        </fieldset>}
        {state && step === 2 && <>
          <div className="wizard-echo"><span className="wizard-echo-label">Your first task</span><span className="wizard-echo-text">{what || selected?.label}</span></div>
          <AgentCards agents={agents} launching={launchBusy || launching} onLaunch={(id) => void launch(id)} />
          <SetupProgress steps={steps} installing={installing} />
          {!agents.some((agent) => agent.ready) && <p className="wizard-industry-note">No agent is ready yet. You can wait here or pick one later; your goal is saved.</p>}
          <div className="wizard-foot"><button className="btn btn-ghost" disabled={pending} onClick={() => { cancelCandidates(); setStep(1); setError(""); }}><ArrowLeft size={14} /> Edit goal</button>
            <button className="btn btn-ghost" disabled={pending} onClick={onClose}>I'll pick an agent later</button></div>
        </>}
      </div>
    </dialog>
  );
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function validSelection(value: unknown): value is WizardIdea {
  if (!value || typeof value !== "object") return false;
  const selected = value as Record<string, unknown>;
  return ["id", "label", "outcome", "prompt"].every((key) => typeof selected[key] === "string")
    && ["fit_reason", "first_version", "selection_token"].every((key) => selected[key] === undefined || typeof selected[key] === "string")
    && ["assumptions", "unresolved"].every((key) => selected[key] === undefined
      || (Array.isArray(selected[key]) && selected[key].every((item) => typeof item === "string")));
}
