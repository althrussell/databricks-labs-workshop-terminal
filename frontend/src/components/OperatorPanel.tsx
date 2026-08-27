import { useCallback, useEffect, useState } from "react";
import {
  KeyRound,
  LifeBuoy,
  Megaphone,
  RadioTower,
  ShieldOff,
  Users,
} from "lucide-react";
import {
  api,
  CredentialStatus,
  OmnigentAttendee,
  OmnigentTier,
  PresenceUser,
} from "../api";

export default function OperatorPanel() {
  const [phases, setPhases] = useState<string[]>([]);
  const [phase, setPhase] = useState("");
  const [users, setUsers] = useState<PresenceUser[]>([]);
  const [sessionCount, setSessionCount] = useState(0);
  const [credential, setCredential] = useState<CredentialStatus | null>(null);
  const [tier, setTier] = useState<OmnigentTier | null>(null);
  const [working, setWorking] = useState("");
  const [message, setMessage] = useState("");
  const [level, setLevel] = useState("info");
  const [status, setStatus] = useState("");
  const [denied, setDenied] = useState(false);

  const refresh = useCallback(() => {
    api
      .adminState()
      .then((state) => {
        setPhases(state.phases);
        setPhase(state.phase);
      })
      .catch((e) => setDenied(e.status === 403));
    api
      .adminPresence()
      .then((data) => {
        setUsers(data.users);
        setSessionCount(data.session_count);
        setCredential(data.credential);
      })
      .catch(() => undefined);
    api.adminOmnigentTier().then(setTier).catch(() => undefined);
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 10000);
    return () => clearInterval(interval);
  }, [refresh]);

  if (denied) {
    return (
      <div className="operator-denied">
        Operator access requires membership in the workshop admin group.
      </div>
    );
  }

  async function changePhase(next: string) {
    await api.adminPhase(next);
    setPhase(next);
    setStatus(`Phase set to ${next}`);
  }

  async function setTierEnabled(enabled: boolean) {
    setWorking("tier");
    try {
      const next = await api.adminSetOmnigentTier(enabled);
      setTier((prev) => (prev ? { ...prev, enabled: next.enabled } : prev));
      setStatus(
        next.enabled
          ? "Omnigent restored — cards return on the attendees' next poll."
          : "Omnigent demoted fleet-wide. Everyone keeps Claude and Codex."
      );
    } finally {
      setWorking("");
      refresh();
    }
  }

  async function recover(email?: string) {
    setWorking(email ?? "all");
    try {
      const result = await api.adminRecover(email);
      const target = email ?? "everyone";
      setStatus(
        result.recovered.length
          ? `Recovered ${result.recovered.join(", ")}.`
          : `No credential recovered for ${target} — the tab has to be open and signed in.`
      );
    } finally {
      setWorking("");
      refresh();
    }
  }

  async function sendBroadcast() {
    if (!message.trim()) return;
    await api.adminBroadcast(message.trim(), level, 300);
    setMessage("");
    setStatus("Broadcast sent");
  }

  return (
    <div className="operator-panel">
      {credential && (
        <section className="op-card">
          <h2>
            <KeyRound size={16} /> Credential health
          </h2>
          <div className="credential-status">
            <span className={`dot ${credential.healthy ? "dot-online" : "dot-offline"}`} />
            <strong>{credentialLabel(credential)}</strong>
            <span className="credential-source">source: {credential.source}</span>
            {credential.token_expires_in != null && (
              <span className="credential-source">
                token refreshes in {Math.max(0, Math.round(credential.token_expires_in / 60))}m
              </span>
            )}
          </div>
          {credential.state !== "rotating" && credential.last_error && (
            <p className="credential-error">{credential.last_error}</p>
          )}
        </section>
      )}

      {tier?.remote && (
        <section className="op-card">
          <h2>
            <ShieldOff size={16} /> Omnigent plane
          </h2>
          <p className="op-note">
            Every Omnigent harness shares one credential plane, so they fail
            together. Demoting withdraws those cards for everyone and leaves
            bare Claude and Codex, which run on the app credential and
            cannot fail this way.
          </p>
          <div className="op-actions">
            <button
              className={tier.enabled ? "danger-btn" : "primary-btn"}
              disabled={working === "tier"}
              onClick={() => setTierEnabled(!tier.enabled)}
            >
              {tier.enabled ? "Demote Omnigent (fleet)" : "Restore Omnigent"}
            </button>
            <button
              className="secondary-btn"
              disabled={working === "all"}
              onClick={() => recover()}
            >
              <LifeBuoy size={13} /> Recover everyone
            </button>
            {!tier.enabled && (
              <span className="op-warn">Demoted — attendees see bare CLIs only.</span>
            )}
          </div>
          <table className="presence-table">
            <thead>
              <tr>
                <th></th>
                <th>Attendee</th>
                <th>Sign-in</th>
                <th>Host</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {tier.attendees.map((a) => (
                <tr key={a.email}>
                  <td>
                    <span className={`dot ${a.obo.fresh ? "dot-online" : "dot-warn"}`} />
                  </td>
                  <td>{a.email}</td>
                  <td>{signInLabel(a)}</td>
                  <td>{a.host.status}</td>
                  <td>
                    <button
                      className="secondary-btn"
                      disabled={working === a.email}
                      onClick={() => recover(a.email)}
                    >
                      Recover
                    </button>
                  </td>
                </tr>
              ))}
              {tier.attendees.length === 0 && (
                <tr>
                  <td colSpan={5} className="presence-empty">
                    No attendees yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <section className="op-card">
        <h2>
          <RadioTower size={16} /> Workshop phase
        </h2>
        <div className="phase-buttons">
          {phases.map((p) => (
            <button
              key={p}
              className={`phase-btn ${p === phase ? "phase-btn-active" : ""}`}
              onClick={() => changePhase(p)}
            >
              {p}
            </button>
          ))}
        </div>
      </section>

      <section className="op-card">
        <h2>
          <Megaphone size={16} /> Broadcast to all attendees
        </h2>
        <div className="broadcast-row">
          <input
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && sendBroadcast()}
            placeholder="Labs close in 10 minutes…"
          />
          <select value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="info">info</option>
            <option value="success">success</option>
            <option value="warning">warning</option>
          </select>
          <button className="primary-btn" onClick={sendBroadcast}>
            Send
          </button>
        </div>
      </section>

      <section className="op-card">
        <h2>
          <Users size={16} /> Attendees ({users.filter((u) => u.online).length} online,{" "}
          {sessionCount} terminals)
        </h2>
        <table className="presence-table">
          <thead>
            <tr>
              <th></th>
              <th>Attendee</th>
              <th>Terminals</th>
              <th>CLI configured</th>
              <th>Last activity</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <tr key={user.email}>
                <td>
                  <span className={`dot ${user.online ? "dot-online" : "dot-offline"}`} />
                </td>
                <td>{user.email}</td>
                <td>
                  {user.sessions.map((s) => s.label).join(", ") || "—"}
                </td>
                <td>{user.cli_ready ? "✓" : "—"}</td>
                <td>{user.last_seen ? timeAgo(user.last_seen) : "—"}</td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={5} className="presence-empty">
                  No attendees yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      {status && <div className="op-status">{status}</div>}
    </div>
  );
}

function credentialLabel(c: CredentialStatus): string {
  switch (c.state) {
    case "rotating":
      return "Rotating short-lived tokens";
    case "degraded":
      return "Degraded — static token, not rotating";
    case "unhealthy":
      return "Unhealthy — credential not usable";
    default:
      return c.configured ? "Configured (checking…)" : "Not configured";
  }
}

/** How long this attendee's Omnigent access has left, in words. */
function signInLabel(a: OmnigentAttendee): string {
  if (!a.obo.present) return "never captured";
  if (!a.obo.fresh) return "stale — tab closed or expired";
  if (a.obo.expires_in == null) return "signed in";
  return `${Math.max(0, Math.round(a.obo.expires_in / 60))}m left`;
}

function timeAgo(epoch: number): string {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}
