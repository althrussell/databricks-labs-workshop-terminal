import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronDown,
  Database,
  Dices,
  Loader2,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { api, AgentInfo, WizardIdea, WizardState } from "../api";
import AgentCards, { SetupProgress, SetupSteps, SETUP_POLL_MS } from "./AgentCards";
import { humanIndustry, industryOf, isGenericIdea } from "../wizard";

const INTENT_LABELS: Record<string, string> = {
  business_problem: "Solving a real problem from work",
  evaluation: "Seeing whether Databricks can do this",
  learning: "Learning how this works",
  fun: "Building something fun",
};

const STACKS = [
  "Snowflake",
  "BigQuery",
  "Redshift",
  "Synapse / Fabric",
  "SQL Server",
  "Oracle",
  "Postgres",
  "Kafka",
  "dbt",
  "Airflow",
  "Tableau",
  "Power BI",
  "Spreadsheets",
];

interface Props {
  agents: AgentInfo[];
  launching: string | null;
  onLaunch: (agentId: string, starterPrompt: string) => void;
  onClose: () => void;
}

/** The opening wizard: two visible steps, always skippable, ends on a running agent.
 *
 * Step one is what they will build plus the industry chip. Optional context
 * (intent, persona, stack) is collapsed behind a toggle on that same screen so
 * "1 of 2" stays honest. Step two is the agent picker.
 */
export default function Wizard({ agents, launching, onLaunch, onClose }: Props) {
  const [step, setStep] = useState(1);
  const [state, setState] = useState<WizardState | null>(null);
  const [what, setWhat] = useState("");
  const [industry, setIndustry] = useState("");
  const [intent, setIntent] = useState("");
  const [ideaId, setIdeaId] = useState("");
  const [stack, setStack] = useState<string[]>([]);
  const [persona, setPersona] = useState("");
  const [showIdeas, setShowIdeas] = useState(false);
  const [showContext, setShowContext] = useState(false);
  const [ideas, setIdeas] = useState<WizardIdea[]>([]);
  const [saving, setSaving] = useState(false);
  const [starterPrompt, setStarterPrompt] = useState("");
  const [steps, setSteps] = useState<SetupSteps | null>(null);
  const [installing, setInstalling] = useState(false);
  const [llmBusy, setLlmBusy] = useState(false);
  const [inferredIndustry, setInferredIndustry] = useState(false);

  // Held in a ref so the load effect below never lists it as a dependency.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  const suggestTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const suggestAbort = useRef<AbortController | null>(null);
  const industryRef = useRef(industry);
  industryRef.current = industry;

  /* Runs once, on mount, and must never run again.
   *
   * This effect seeds the state the attendee then edits, so re-running it is
   * data loss rather than a refresh: it would overwrite the sentence being
   * typed with the empty one on the server, deselect a chosen card, and hand
   * back a freshly shuffled grid mid-read. A dependency on anything the parent
   * re-creates each render — `onClose` was an inline arrow, and Home re-renders
   * every few seconds while it polls agent install progress — makes that happen
   * on a timer. */
  useEffect(() => {
    api
      .wizard()
      .then((s) => {
        setState(s);
        const locked =
          s.industries.length === 1
            ? s.industries[0]
            : s.brief.industry || s.default_industry;
        setIndustry(locked);
        setIdeas(s.ideas);
        setWhat(s.brief.what_building);
        setIntent(s.brief.intent);
        setIdeaId(s.brief.idea_id);
        setStack(s.brief.current_stack);
        setPersona(s.brief.persona);
        if (s.llm_wizard?.enabled) {
          queueSuggest(s.brief.what_building, locked);
        }
      })
      .catch(() => onCloseRef.current());
    return () => {
      if (suggestTimer.current) clearTimeout(suggestTimer.current);
      suggestAbort.current?.abort();
    };
    // queueSuggest is stable enough for mount-only; listing it would retrigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The agent cards on step two are the real picker, so they need the same
  // install progress the landing page shows. Polling starts with the wizard
  // rather than on arrival at step two: by the time someone gets there the
  // install is usually done, and a spinner that appears late reads as a stall.
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const poll = () =>
      api
        .setupStatus()
        .then((s) => {
          setSteps(s.steps);
          setInstalling(s.installing);
          if (!s.installing && timer) clearInterval(timer);
        })
        .catch(() => undefined);
    poll();
    timer = setInterval(poll, SETUP_POLL_MS);
    return () => {
      if (timer) clearInterval(timer);
    };
  }, []);

  // Esc is the same door as Skip, not a different one. A modal that can be
  // dismissed without recording that it was dismissed comes back on the next
  // reload, which in a workshop room means it comes back during the demo.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") skip();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const selectedIdea = useMemo(
    () => ideas.find((i) => i.id === ideaId) ?? null,
    [ideas, ideaId]
  );

  const stacks = state?.stacks?.length ? state.stacks : STACKS;
  const labels =
    state?.intent_labels && Object.keys(state.intent_labels).length
      ? state.intent_labels
      : INTENT_LABELS;

  function pickIdea(idea: WizardIdea) {
    setIdeaId(idea.id);
    setWhat(idea.outcome);
    setShowIdeas(false);
    setIndustry(industryOf(idea));
    setInferredIndustry(false);
    // The card already knows why someone would build it, so carrying its intent
    // forward means optional context is a confirmation rather than another question.
    if (!intent && idea.intents.length > 0) setIntent(idea.intents[0]);
  }

  function surpriseMe() {
    api
      .wizardSurprise(industry)
      .then((res) => {
        if (res.idea) pickIdea(res.idea);
      })
      .catch(() => {
        if (ideas.length === 0) return;
        pickIdea(ideas[Math.floor(Math.random() * ideas.length)]);
      });
  }

  function queueSuggest(nextText: string, nextIndustry: string) {
    if (!state?.llm_wizard?.enabled && state !== null) return;
    if (suggestTimer.current) clearTimeout(suggestTimer.current);
    suggestTimer.current = setTimeout(() => {
      runSuggest(nextText, nextIndustry);
    }, 400);
  }

  function runSuggest(nextText: string, nextIndustry: string) {
    suggestAbort.current?.abort();
    const ctl = new AbortController();
    suggestAbort.current = ctl;
    setLlmBusy(true);
    api
      .wizardSuggest({ text: nextText, industry: nextIndustry }, ctl.signal)
      .then((res) => {
        if (ctl.signal.aborted) return;
        setIdeas(res.ideas);
        if (res.industry && res.industry !== industryRef.current) {
          setIndustry(res.industry);
          setInferredIndustry(true);
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (!ctl.signal.aborted) setLlmBusy(false);
      });
  }

  // The chip has to be sent, not just set: the brief has not been saved yet, so
  // the server has no other way to know which industry the grid is being asked
  // for, and a filter that visibly does nothing is worse than no filter.
  function refreshIdeas(nextIndustry: string) {
    setIndustry(nextIndustry);
    setInferredIndustry(false);
    api
      .wizard(nextIndustry)
      .then((s) => setIdeas(s.ideas))
      .catch(() => undefined);
    if (state?.llm_wizard?.enabled) {
      queueSuggest(what, nextIndustry);
    }
  }

  function toggleIndustry(ind: string) {
    refreshIdeas(industry === ind ? "" : ind);
  }

  function toggleStack(item: string) {
    setStack((prev) =>
      prev.includes(item) ? prev.filter((s) => s !== item) : [...prev, item]
    );
  }

  async function skip() {
    // Recorded, not just closed. "Seen" lives on the server so a reload, a
    // second tab or a reconnect does not re-present it. Do not wait for an
    // in-flight model call — Skip is an answer, not a pause.
    suggestAbort.current?.abort();
    try {
      await api.saveWizard({ skipped: true });
    } catch {
      /* closing must never fail on a network blip */
    }
    onClose();
  }

  /** Save on leaving step one, not step two.
   *
   * By the end of step one the attendee has said everything the discovery
   * record needs — including a confirmed industry chip. Step two is choosing
   * an agent, and someone who wanders back to the landing page to launch from
   * there must not lose what they just told us.
   */
  async function saveAndContinue() {
    setSaving(true);
    suggestAbort.current?.abort();
    try {
      const res = await api.saveWizard({
        what_building: what,
        industry,
        industry_stated: Boolean(industry) || Boolean(ideaId),
        intent,
        idea_id: ideaId,
        current_stack: stack,
        persona,
      });
      setStarterPrompt(res.starter_prompt);
      setStep(2);
    } catch {
      // Losing the brief is bad; blocking the attendee behind a failed save is
      // worse. Let them through to the agents either way — but with a prompt.
      // Step two launches with whatever is in this state, and an empty string
      // there drops them at the blank cursor this whole wizard exists to avoid,
      // having just watched their answers scroll past.
      setStarterPrompt(selectedIdea ? selectedIdea.prompt : what.trim());
      setStep(2);
    } finally {
      setSaving(false);
    }
  }

  if (!state) {
    return (
      <div className="modal-backdrop">
        <div className="modal modal-wide wizard">
          <Loader2 size={20} className="spin" />
        </div>
      </div>
    );
  }

  const canContinue = what.trim().length > 0 || ideaId !== "";
  const industries = state.industries;
  const singleSchema = industries.length === 1 ? industries[0] : "";

  return (
    <div className="modal-backdrop">
      <div
        className="modal modal-wide wizard"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="wizard-head">
          <span className="wizard-step-count">Step {step} of 2</span>
          <button className="wizard-skip" onClick={skip}>
            Skip
          </button>
        </div>

        {step === 1 && (
          <div className="wizard-body">
            <h2 className="wizard-title">
              What will you <span className="hero-accent">build</span> today?
            </h2>
            <p className="wizard-sub">
              One sentence is plenty. Your agent picks it up from here — you don't
              need to know how any of it works.
            </p>

            <textarea
              className="wizard-input"
              rows={3}
              autoFocus
              placeholder="e.g. a dashboard showing which parts fail early across our fleet"
              value={what}
              onChange={(e) => {
                const next = e.target.value;
                setWhat(next);
                // Typing over a picked card makes it theirs, not the card's.
                if (ideaId) setIdeaId("");
                if (state.llm_wizard?.enabled) queueSuggest(next, industry);
              }}
            />

            {industries.length > 0 && (
              <div className="wizard-filter">
                {singleSchema ? (
                  <p className="wizard-industry-note">
                    This room's demo data is {humanIndustry(singleSchema)}.
                  </p>
                ) : (
                  <>
                    <span className="wizard-filter-label">Demo data</span>
                    <div className="wizard-chips">
                      {industries.map((ind) => (
                        <button
                          key={ind}
                          className={`hero-chip ${
                            industry === ind ? "hero-chip-active" : ""
                          }`}
                          onClick={() => toggleIndustry(ind)}
                        >
                          {humanIndustry(ind)}
                        </button>
                      ))}
                    </div>
                  </>
                )}
                {industry && industries.length > 1 && (
                  <p className="wizard-industry-note">
                    Using the {humanIndustry(industry)} demo data
                    {inferredIndustry ? " — change it if that's wrong." : "."}
                  </p>
                )}
              </div>
            )}

            <div className="wizard-actions-inline">
              <button
                className="wizard-ghost"
                onClick={() => setShowIdeas((v) => !v)}
              >
                <Sparkles size={13} />{" "}
                {showIdeas ? "Hide ideas" : "I'm not sure yet — show me ideas"}
              </button>
              <button className="wizard-ghost" onClick={surpriseMe}>
                <Dices size={13} /> Surprise me
              </button>
              {llmBusy && (
                <span className="wizard-llm-status">
                  <Loader2 size={12} className="spin" /> Matching ideas
                </span>
              )}
            </div>

            {showIdeas && (
              <div className="wizard-ideas">
                <div className="wizard-idea-grid">
                  {ideas.map((idea) => (
                    <button
                      key={idea.id}
                      className={`wizard-idea ${
                        ideaId === idea.id ? "wizard-idea-active" : ""
                      }`}
                      onClick={() => pickIdea(idea)}
                    >
                      <span className="wizard-idea-label">{idea.label}</span>
                      <span className="wizard-idea-outcome">{idea.outcome}</span>
                      {isGenericIdea(idea) && (
                        <span className="wizard-idea-generic">
                          works with any data
                        </span>
                      )}
                      {/* Only ever shown on cards whose tables the server has
                          verified, so it is a fact rather than a hope. */}
                      {idea.demo_tables.length > 0 && (
                        <span className="wizard-idea-badge">
                          <Database size={10} /> Data ready
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}

            <button
              className="wizard-context-toggle"
              onClick={() => setShowContext((v) => !v)}
            >
              <ChevronDown
                size={13}
                className={showContext ? "wizard-chevron-open" : ""}
              />
              A little context (optional)
            </button>

            {showContext && (
              <div className="wizard-context">
                <div className="wizard-field">
                  <span className="wizard-field-label">Why are you building it?</span>
                  <div className="wizard-chips">
                    {state.intents.map((value) => (
                      <button
                        key={value}
                        className={`hero-chip ${
                          intent === value ? "hero-chip-active" : ""
                        }`}
                        onClick={() => setIntent(intent === value ? "" : value)}
                      >
                        {labels[value] ?? value}
                      </button>
                    ))}
                  </div>
                </div>

                <div className="wizard-field">
                  <span className="wizard-field-label">
                    How should your agent explain things?
                  </span>
                  <div className="wizard-chips">
                    <button
                      className={`hero-chip ${
                        persona === "business" ? "hero-chip-active" : ""
                      }`}
                      onClick={() => setPersona("business")}
                    >
                      Plain language
                    </button>
                    <button
                      className={`hero-chip ${
                        persona === "technical" ? "hero-chip-active" : ""
                      }`}
                      onClick={() => setPersona("technical")}
                    >
                      I'm technical
                    </button>
                  </div>
                </div>

                <div className="wizard-field">
                  <span className="wizard-field-label">
                    What do you use today? <em>(optional)</em>
                  </span>
                  <div className="wizard-chips">
                    {stacks.map((item) => (
                      <button
                        key={item}
                        className={`hero-chip ${
                          stack.includes(item) ? "hero-chip-active" : ""
                        }`}
                        onClick={() => toggleStack(item)}
                      >
                        {item}
                      </button>
                    ))}
                  </div>
                </div>

                {state.capture_enabled && (
                  <div className="wizard-notice">
                    <ShieldCheck size={13} />
                    <span>
                      Your host sees what people are building so they can follow up
                      after the event. You can view or withdraw yours any time from
                      the insights panel.
                    </span>
                  </div>
                )}
              </div>
            )}

            <div className="wizard-foot">
              <span />
              <button
                className="btn btn-primary"
                disabled={!canContinue || saving}
                onClick={saveAndContinue}
              >
                {saving ? <Loader2 size={13} className="spin" /> : null} Next
              </button>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="wizard-body">
            <h2 className="wizard-title">Pick your agent and go</h2>
            <p className="wizard-sub">
              They're all set up already. Any of them can build this — pick one
              and it starts with what you just told us.
            </p>

            <div className="wizard-echo">
              <span className="wizard-echo-label">First up</span>
              <span className="wizard-echo-text">
                {what || selectedIdea?.label}
              </span>
            </div>

            <AgentCards
              agents={agents}
              launching={launching}
              onLaunch={(id) => onLaunch(id, starterPrompt)}
            />
            <SetupProgress steps={steps} installing={installing} />
            <p className="wizard-enter-hint">Press Enter to start</p>

            <div className="wizard-foot">
              <button className="btn btn-ghost" onClick={() => setStep(1)}>
                <ArrowLeft size={13} /> Back
              </button>
              <button className="btn btn-ghost" onClick={onClose}>
                I'll pick later
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
