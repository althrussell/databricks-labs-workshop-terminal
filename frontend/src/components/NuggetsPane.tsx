import { useCallback, useEffect, useState } from "react";
import { ChevronRight, ExternalLink, Lightbulb, Pin, Sparkle } from "lucide-react";
import { marked } from "marked";
import { api, Nugget } from "../api";
import { onAppEvent } from "../events";

interface Props {
  collapsed: boolean;
  onToggle: () => void;
}

export default function NuggetsPane({ collapsed, onToggle }: Props) {
  const [nuggets, setNuggets] = useState<Nugget[]>([]);
  const [phase, setPhase] = useState("");

  const refresh = useCallback(() => {
    api
      .nuggets()
      .then((data) => {
        setNuggets(data.nuggets);
        setPhase(data.phase);
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

  if (collapsed) {
    return (
      <button className="nuggets-collapsed" onClick={onToggle} title="Show insights">
        <Lightbulb size={18} />
      </button>
    );
  }

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
      <div className="nuggets-list">
        {nuggets.length === 0 && (
          <div className="nugget-empty">Insights will appear here as the workshop progresses.</div>
        )}
        {nuggets.map((nugget) => (
          <article
            key={nugget.id}
            className={`nugget ${nugget.pinned ? "nugget-pinned" : ""} ${
              nugget.matched_topic ? "nugget-matched" : ""
            }`}
          >
            {nugget.matched_topic && (
              <div className="nugget-matched-label">
                <Sparkle size={10} /> spotted in your session
              </div>
            )}
            <h3>
              {nugget.pinned && <Pin size={12} />}
              {nugget.title}
            </h3>
            <div
              className="nugget-body"
              dangerouslySetInnerHTML={{ __html: marked.parse(nugget.markdown) as string }}
            />
            {nugget.link && (
              <a className="nugget-link" href={nugget.link.url} target="_blank" rel="noreferrer">
                {nugget.link.label} <ExternalLink size={12} />
              </a>
            )}
          </article>
        ))}
      </div>
    </aside>
  );
}
