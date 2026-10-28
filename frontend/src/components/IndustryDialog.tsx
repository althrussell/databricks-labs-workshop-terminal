import { useEffect, useRef, useState } from "react";
import { Building2, Loader2 } from "lucide-react";
import { api, WizardBrief, WizardState } from "../api";
import { trapDialogTab } from "../dialog";
import { humanIndustry } from "../wizard";

interface Props {
  onClose: (skipped?: boolean) => void;
  onSaved?: (brief: WizardBrief) => void;
}

export default function IndustryDialog({ onClose, onSaved }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [state, setState] = useState<WizardState | null>(null);
  const [industry, setIndustry] = useState("");
  const [industryStated, setIndustryStated] = useState(false);
  const [other, setOther] = useState("");
  const [otherOpen, setOtherOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const element = dialog.current;
    const returnFocus = document.activeElement as HTMLElement | null;
    const priorOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    element?.showModal();

    let active = true;
    api
      .wizard()
      .then((loaded) => {
        if (!active) return;
        setState(loaded);
        const currentIndustry =
          loaded.brief.industry ||
          ((loaded.brief.revision ?? 0) === 0 && !loaded.brief.seen ? loaded.default_industry : "");
        setIndustry(currentIndustry);
        setIndustryStated(Boolean(loaded.brief.industry_stated));
      })
      .catch((caught) => {
        if (active) setError(caught instanceof Error ? caught.message : String(caught));
      });

    return () => {
      active = false;
      element?.close();
      document.body.style.overflow = priorOverflow;
      if (returnFocus?.isConnected) returnFocus.focus();
    };
  }, []);

  const labelIndustry = (value: string) => state?.industry_labels?.[value] ?? humanIndustry(value);

  async function save(selectedIndustry: string, stated: boolean) {
    setSaving(true);
    setError("");
    try {
      const saved = await api.saveWizard({
        operation: "industry",
        industry: selectedIndustry,
        industry_stated: stated && Boolean(selectedIndustry),
        expected_revision: state?.brief.revision ?? 0,
      });
      onSaved?.(saved.brief);
      onClose(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }

  async function skip() {
    setSaving(true);
    try {
      const saved = await api.saveWizard({
        operation: "industry",
        industry: state?.brief.industry_stated ? state.brief.industry : "",
        industry_stated: Boolean(state?.brief.industry_stated),
        expected_revision: state?.brief.revision ?? 0,
      });
      onSaved?.(saved.brief);
    } catch {
      /* Dismissal in current tab still holds if network write fails. */
    } finally {
      setSaving(false);
      onClose(true);
    }
  }

  return (
    <dialog
      ref={dialog}
      className="modal preferences-dialog"
      aria-labelledby="industry-dialog-title"
      aria-describedby="industry-dialog-desc"
      onCancel={(ev) => {
        ev.preventDefault();
        void skip();
      }}
      onKeyDown={trapDialogTab}
    >
      <div className="wizard-head">
        <span className="wizard-step-count" style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
          <Building2 size={14} aria-hidden="true" /> Industry context
        </span>
        <button type="button" className="wizard-skip" onClick={() => void skip()}>
          Skip for now
        </button>
      </div>
      <div className="wizard-body">
        <h2 id="industry-dialog-title" className="wizard-title">
          What industry do you work in?
        </h2>
        <p id="industry-dialog-desc" className="wizard-sub">
          Pick your industry so your coding agent tailors its domain knowledge and demo tables to your field.
        </p>

        {error && (
          <div className="wizard-error" role="alert">
            <p>{error}</p>
          </div>
        )}

        {!state && !error && (
          <p role="status">
            <Loader2 size={16} className="spin" /> Loading industries…
          </p>
        )}

        {state && (
          <fieldset className="wizard-fields" disabled={saving} aria-label="Industry selection">
            <div className="wizard-field">
              <div className="wizard-chips">
                {state.industries.map((value) => {
                  const activeChip = industry === value && industryStated;
                  return (
                    <button
                      key={value}
                      type="button"
                      className={`hero-chip ${activeChip ? "hero-chip-active" : ""}`}
                      aria-pressed={activeChip}
                      onClick={() => {
                        const next = activeChip ? "" : value;
                        setIndustry(next);
                        setIndustryStated(Boolean(next));
                        setOtherOpen(false);
                      }}
                    >
                      {labelIndustry(value)}
                    </button>
                  );
                })}
                <button
                  type="button"
                  className="hero-chip"
                  aria-expanded={otherOpen}
                  onClick={() => setOtherOpen(!otherOpen)}
                >
                  Other industry
                </button>
                {industry && (
                  <button
                    type="button"
                    className="hero-chip"
                    onClick={() => {
                      setIndustry("");
                      setIndustryStated(false);
                    }}
                  >
                    Clear
                  </button>
                )}
              </div>

              {otherOpen && (
                <div className="wizard-other-row">
                  <label htmlFor="industry-dialog-other">Your industry</label>
                  <input
                    id="industry-dialog-other"
                    className="wizard-other-input"
                    value={other}
                    maxLength={64}
                    placeholder="e.g. Mining, Aviation, Agribusiness"
                    onChange={(ev) => setOther(ev.target.value)}
                  />
                  <button
                    type="button"
                    className="btn btn-ghost"
                    onClick={() => {
                      const trimmed = other.trim();
                      setIndustry(trimmed);
                      setIndustryStated(Boolean(trimmed));
                      setOtherOpen(false);
                    }}
                  >
                    Use this industry
                  </button>
                </div>
              )}
              {industry && !industryStated && (
                <p className="wizard-industry-note">
                  Suggested for this workshop: <strong>{labelIndustry(industry)}</strong>. Click Save industry to confirm, or choose another.
                </p>
              )}
            </div>

            <div className="wizard-foot">
              <span>You can change this anytime from the top bar.</span>
              <button
                type="button"
                className="btn btn-primary"
                disabled={saving || !industry.trim()}
                onClick={() => void save(industry.trim(), true)}
              >
                {saving && <Loader2 size={14} className="spin" />} Save industry
              </button>
            </div>
          </fieldset>
        )}
      </div>
    </dialog>
  );
}
