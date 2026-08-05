import { useEffect, useRef, useState } from "react";
import { MessageSquare, Send, X } from "lucide-react";
import { api, HelpMessage } from "../api";
import { onAppEvent } from "../events";

export default function HelpChatPanel({
  open,
  onClose,
  unread,
  onClearUnread,
  raised,
  onRaisedChange,
}: {
  open: boolean;
  onClose: () => void;
  unread: number;
  onClearUnread: () => void;
  raised: boolean;
  onRaisedChange: (raised: boolean) => void;
}) {
  const [messages, setMessages] = useState<HelpMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refresh = async () => {
    try {
      const thread = await api.helpThread();
      setMessages(thread.messages ?? []);
      onRaisedChange(thread.raised);
    } catch {
      /* fail-soft */
    }
  };

  useEffect(() => {
    if (open) {
      void refresh();
      onClearUnread();
    }
  }, [open]);

  useEffect(() => {
    return onAppEvent((event) => {
      if (event.t === "help_message") {
        setMessages((prev) => {
          if (prev.some((m) => m.message_id === event.message_id)) return prev;
          return [
            ...prev,
            {
              message_id: event.message_id,
              help_request_id: event.help_request_id,
              sender_role: event.sender_role,
              sender: event.sender,
              body: event.body,
              created_at: event.created_at,
            },
          ];
        });
      }
      if (event.t === "help_state") {
        onRaisedChange(event.raised);
      }
    });
  }, [onRaisedChange]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, open]);

  const send = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      const result = await api.postHelpMessage(text);
      setDraft("");
      onRaisedChange(result.raised);
      setMessages((prev) => {
        if (prev.some((m) => m.message_id === result.message.message_id)) return prev;
        return [...prev, result.message];
      });
    } catch {
      /* fail-soft */
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  return (
    <aside className="help-chat-panel" aria-label="Help chat with operators">
      <div className="help-chat-header">
        <div className="help-chat-title">
          <MessageSquare size={16} />
          <span>Ask operators</span>
          {raised ? <span className="help-chat-raised-pill">Hand raised</span> : null}
          {unread > 0 ? <span className="help-chat-unread">{unread}</span> : null}
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close help chat">
          <X size={16} />
        </button>
      </div>
      <div className="help-chat-messages">
        {messages.length === 0 ? (
          <p className="help-chat-empty">
            Ask a question or raise your hand. Operators will reply here.
          </p>
        ) : (
          messages.map((m) => (
            <div
              key={m.message_id}
              className={`help-chat-bubble help-chat-${m.sender_role === "operator" ? "operator" : "attendee"}`}
            >
              <div className="help-chat-meta">
                {m.sender_role === "operator" ? m.sender || "Operator" : "You"}
              </div>
              <div className="help-chat-body">{m.body}</div>
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
      <div className="help-chat-composer">
        <input
          type="text"
          value={draft}
          maxLength={2000}
          placeholder="Message the operators…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void send();
          }}
        />
        <button
          type="button"
          className="primary-btn"
          disabled={busy || !draft.trim()}
          onClick={() => void send()}
          aria-label="Send"
        >
          <Send size={14} />
        </button>
      </div>
    </aside>
  );
}
