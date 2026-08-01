import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronUp, NotebookPen, Trash2 } from "lucide-react";
import { api, DiscoveryRecord } from "../api";

const POLL_MS = 60000;

// What the agent recorded, shown to the person it recorded it about, with a way
// to take it back. Rendered only once something has actually been captured: with
// no records there is nothing to be transparent about, and a notice about a
// feature that has not touched the attendee is noise in a 320px pane.
export default function DiscoveryNotice() {
  const [records, setRecords] = useState<DiscoveryRecord[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [withdrawing, setWithdrawing] = useState<string | null>(null);

  const refresh = useCallback(() => {
    api
      .myDiscovery()
      .then((data) => setRecords(data.enabled ? data.records : []))
      .catch(() => {
        /* capture is optional; a fetch failure must not disturb the lab */
      });
  }, []);

  useEffect(() => {
    refresh();
    // Records appear mid-conversation as the agent elicits them, so the notice
    // has to arrive without a reload the attendee has no reason to perform.
    const interval = setInterval(refresh, POLL_MS);
    return () => clearInterval(interval);
  }, [refresh]);

  const withdraw = (recordId: string) => {
    setWithdrawing(recordId);
    api
      .withdrawDiscovery(recordId)
      .then(() => setRecords((current) => current.filter((r) => r.record_id !== recordId)))
      .catch(refresh) // Server is the authority on what is still held.
      .finally(() => setWithdrawing(null));
  };

  if (records.length === 0) return null;

  return (
    <section className="discovery-notice">
      <button
        className="discovery-summary"
        onClick={() => setExpanded((open) => !open)}
        aria-expanded={expanded}
      >
        <NotebookPen size={13} />
        <span>
          {records.length} note{records.length === 1 ? "" : "s"} about what you're building
        </span>
        {expanded ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
      </button>
      {expanded && (
        <div className="discovery-body">
          <p className="discovery-explainer">
            Your assistant noted these so the Databricks team can follow up on what you
            asked for. Remove anything you'd rather not share.
          </p>
          {records.map((record) => (
            <article key={record.record_id} className="discovery-record">
              <header>
                <h4>{record.use_case_title || record.goal || "Use case"}</h4>
                <button
                  className="discovery-withdraw"
                  onClick={() => withdraw(record.record_id)}
                  disabled={withdrawing === record.record_id}
                  title="Remove this note"
                >
                  <Trash2 size={12} />
                  {withdrawing === record.record_id ? "Removing…" : "Remove"}
                </button>
              </header>
              {record.use_case_summary && <p>{record.use_case_summary}</p>}
              <DiscoveryFacts label="Stack" values={record.current_stack} />
              <DiscoveryFacts label="Databricks" values={record.databricks_products} />
              <DiscoveryFacts label="Blockers" values={record.blockers} />
              {record.timeline && (
                <p className="discovery-meta">Timeline: {record.timeline}</p>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function DiscoveryFacts({ label, values }: { label: string; values?: string[] }) {
  if (!values || values.length === 0) return null;
  return (
    <p className="discovery-meta">
      {label}: {values.join(", ")}
    </p>
  );
}
