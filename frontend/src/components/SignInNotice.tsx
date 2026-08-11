import { RefreshCw } from "lucide-react";
import { OboStatus } from "../api";
import { signInNotice } from "../signin";

interface Props {
  /** Whether this deployment has an Omnigent plane at all. */
  omnigentEnabled: boolean;
  obo: OboStatus | undefined;
  onReload: () => void;
}

/**
 * The tab rule, on screen for as long as it applies.
 *
 * Saying it once during the intro is not enough: people join late, take breaks,
 * and nobody remembers a housekeeping note an hour later. So it is stated twice
 * — quietly and permanently while things are fine, and unmissably the moment
 * the sign-in actually goes stale.
 *
 * Never a modal. Bare Claude, Codex and Terminal keep working through all of
 * this, and covering them would take away the one thing that still does.
 */
export default function SignInNotice({ omnigentEnabled, obo, onReload }: Props) {
  const state = signInNotice(omnigentEnabled, obo);
  if (state.kind === "none") return null;

  if (state.kind === "expired") {
    return (
      <div className="banner banner-blocking">
        <span>
          <strong>Your Databricks sign-in has expired.</strong> Reload this tab to
          sign in again — it takes a second and you will not lose any work.
          Claude, Codex and Terminal keep working meanwhile.
        </span>
        <button className="banner-action" onClick={onReload}>
          <RefreshCw size={13} /> Reload
        </button>
      </div>
    );
  }

  return (
    <div className={`tab-rule ${state.soon ? "tab-rule-soon" : ""}`}>
      <span>
        Keep this tab open — it is what keeps your Databricks sign-in alive for
        Omnigent agents.
      </span>
      {state.minutes !== null && (
        <span className="tab-rule-clock">
          {state.soon
            ? `Renewing shortly (${Math.max(0, state.minutes)}m left)`
            : `${state.minutes}m of sign-in left`}
        </span>
      )}
    </div>
  );
}
