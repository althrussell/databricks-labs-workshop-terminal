import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronRight, ExternalLink, Lightbulb, Sparkle } from "lucide-react";
import { renderNuggetMarkdown } from "../safeMarkdown";
import { api, Nugget } from "../api";
import { onAppEvent } from "../events";
import DiscoveryNotice from "./DiscoveryNotice";

interface Props {
  collapsed: boolean;
  onToggle: () => void;
  onCertificate: () => void;
  onTryPrompt: (prompt: string) => void;
}

const CARD_HEIGHT = 200; // estimated card footprint incl. gap — drives fit count
const ROTATE_MS = 25000;

// The pane never scrolls: it shows only as many cards as fit, with the most
// relevant (contextually matched, then pinned) first, and rotates the
// remaining slots through the ranked list.
export default function NuggetsPane({ collapsed, onToggle, onCertificate, onTryPrompt }: Props) {
  const [nuggets, setNuggets] = useState<Nugget[]>([]);
  const [phase, setPhase] = useState("");
  const [offset, setOffset] = useState(0);
  const [fitCount, setFitCount] = useState(3);
  const listRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(() => {
    api
      .nuggets()
      .then((data) => {
        setNuggets(data.nuggets);
        setPhase(data.phase);
        setOffset(0); // back to most relevant on every refresh
      })
      .catch(() => {
        /* keep last good content on transient errors */
      });
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 60000);
    const off = onAppEvent((event) => {
      if (event.t === "phase" || event.t === "content_updated") refresh();
    });
    return () => {
      clearInterval(interval);
      off();
    };
  }, [refresh]);

  // Fit: measure the list area and show only whole cards.
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;
    const measure = () =>
      setFitCount(Math.max(1, Math.floor(el.clientHeight / CARD_HEIGHT)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [collapsed]);

  // Rotate the non-anchored slots through the ranked list.
  useEffect(() => {
    if (nuggets.length <= fitCount) return;
    const timer = setInterval(
      () => setOffset((o) => (o + 1) % nuggets.length),
      ROTATE_MS
    );
    return () => clearInterval(timer);
  }, [nuggets.length, fitCount]);

  if (collapsed) {
    return (
      <button className="nuggets-collapsed" onClick={onToggle} title="Show insights">
        <Lightbulb size={18} />
      </button>
    );
  }

  // Anchor: the top nudge or matched card always holds slot 1; the rest rotate.
  const anchor = nuggets.find((n) => n.nudge) ?? nuggets.find((n) => n.matched_topic) ?? null;
  const pool = anchor ? nuggets.filter((n) => n.id !== anchor.id) : nuggets;
  const slots = anchor ? fitCount - 1 : fitCount;
  const rotated =
    pool.length <= slots
      ? pool
      : Array.from({ length: slots }, (_, i) => pool[(offset + i) % pool.length]);
  const visible = anchor ? [anchor, ...rotated] : rotated;

  return (
    <aside className="nuggets-pane">
      <div className="nuggets-header">
        <div className="nuggets-title">
          <Lightbulb size={16} />
          <span>Databricks insights</span>
          {phase && <span className="phase-chip">{phase}</span>}
        </div>
        <button className="icon-btn" onClick={onToggle} title="Collapse">
          <ChevronRight size={16} />
        </button>
      </div>
      <div className="nuggets-list" ref={listRef}>
        {visible.length === 0 && (
          <div className="nugget-empty">Insights will appear here as the workshop progresses.</div>
        )}
        {visible.map((nugget) => (
          <article
            key={nugget.id}
            className={`nugget ${nugget.matched_topic ? "nugget-matched" : ""} ${
              nugget.nudge ? "nugget-nudge" : ""
            }`}
          >
            {nugget.nudge ? (
              <div className="nugget-nudge-label">
                💡 {nugget.cta ?? "While you're paused →"}
              </div>
            ) : (
              nugget.matched_topic && (
                <div className="nugget-matched-label">
                  <Sparkle size={10} /> {nugget.cta ?? "Take the next step →"}
                </div>
              )
            )}
            <h3>{nugget.title}</h3>
            <div
              className="nugget-body"
              dangerouslySetInnerHTML={{ __html: renderNuggetMarkdown(nugget.markdown) }}
            />
            <div className="nugget-actions">
              {nugget.prompt && (
                <button
                  className="nugget-link nugget-link-action"
                  title={nugget.prompt}
                  onClick={() => onTryPrompt(nugget.prompt!)}
                >
                  Type it into my terminal →
                </button>
              )}
              {nugget.link &&
                (nugget.link.url === "#certificate" ? (
                  <button className="nugget-link nugget-link-action" onClick={onCertificate}>
                    {nugget.link.label} <ExternalLink size={12} />
                  </button>
                ) : (
                  <a className="nugget-link" href={nugget.link.url} target="_blank" rel="noreferrer">
                    {nugget.link.label} <ExternalLink size={12} />
                  </a>
                ))}
            </div>
          </article>
        ))}
      </div>
      <DiscoveryNotice />
    </aside>
  );
}
