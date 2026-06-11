import { useCallback, useEffect, useState } from "react";
import { Megaphone, RadioTower, Users } from "lucide-react";
import { api, PresenceUser } from "../api";

export default function OperatorPanel() {
  const [phases, setPhases] = useState<string[]>([]);
  const [phase, setPhase] = useState("");
  const [users, setUsers] = useState<PresenceUser[]>([]);
  const [sessionCount, setSessionCount] = useState(0);
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
      })
      .catch(() => undefined);
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

  async function sendBroadcast() {
    if (!message.trim()) return;
    await api.adminBroadcast(message.trim(), level, 300);
    setMessage("");
    setStatus("Broadcast sent");
  }

  return (
    <div className="operator-panel">
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

function timeAgo(epoch: number): string {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}
