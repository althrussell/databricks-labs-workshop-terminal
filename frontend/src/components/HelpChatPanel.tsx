import { useEffect, useRef, useState } from "react";
import { MessageSquare, Send, ThumbsDown, ThumbsUp, UserRound, X } from "lucide-react";
import { api, HelpMessage, HelpVote } from "../api";
import { onAppEvent } from "../events";
import { bubbleFor, handoffView, latestReply, senderLabel, voteView, type VoteView } from "../helpChat";

export default function HelpChatPanel({
  open,
  onClose,
  unread,
  onClearUnread,
  raised,
  onRaisedChange,
  readOnly = false,
}: {
  open: boolean;
  onClose: () => void;
  unread: number;
  onClearUnread: () => void;
  raised: boolean;
  onRaisedChange: (raised: boolean) => void;
  readOnly?: boolean;
}) {
  const [messages, setMessages] = useState<HelpMessage[]>([]);
  const [feedback, setFeedback] = useState<Record<string, HelpVote>>({});
  const [voting, setVoting] = useState<ReadonlySet<string>>(new Set());
  const [handoffAfter, setHandoffAfter] = useState<string | null>(null);
  const [handoffBusy, setHandoffBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refresh = async () => {
    try {
      const thread = await api.helpThread();
      setMessages(thread.messages ?? []);
      // Merged: votes are final, and a snapshot older than a vote just cast must not hide it.
      setFeedback((prev) => ({ ...prev, ...(thread.feedback ?? {}) }));
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
      if (event.t === "help_feedback") {
        setFeedback((prev) => ({ ...prev, [event.message_id]: event.value }));
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
    if (!text || busy || readOnly) return;
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

  const rate = async (messageId: string, value: HelpVote) => {
    if (voteView(feedback, messageId, { pending: voting.has(messageId), readOnly }).disabled) return;
    setVoting((prev) => new Set(prev).add(messageId));
    try {
      const result = await api.rateHelpMessage(messageId, value);
      setFeedback((prev) => ({ ...prev, [messageId]: result.value }));
    } catch {
      /* fail-soft: the buttons unlock and the attendee can try again */
    } finally {
      setVoting((prev) => {
        const next = new Set(prev);
        next.delete(messageId);
        return next;
      });
    }
  };

  const askForPerson = async () => {
    const reply = latestReply(messages);
    if (!reply || handoffBusy || readOnly) return;
    setHandoffBusy(true);
    try {
      const result = await api.requestPerson();
      setHandoffAfter(reply.message_id);
      onRaisedChange(result.raised);
    } catch {
      /* fail-soft: the button stays so the attendee can ask again */
    } finally {
      setHandoffBusy(false);
    }
  };

  if (!open) return null;

  const handoff = handoffView(messages, { readOnly, requestedAfter: handoffAfter });

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
          messages.map((m) => {
            const bubble = bubbleFor(m.sender_role);
            return (
              <div key={m.message_id} className={`help-chat-bubble help-chat-${bubble}`}>
                <div className="help-chat-meta">
                  {senderLabel(m)}
                  {bubble === "assistant" ? (
                    <span className="ai-badge" title="Answered by AI">AI</span>
                  ) : null}
                </div>
                <div className="help-chat-body">{m.body}</div>
                {bubble === "assistant" ? (
                  <AnswerRating
                    vote={voteView(feedback, m.message_id, {
                      pending: voting.has(m.message_id),
                      readOnly,
                    })}
                    onRate={(value) => void rate(m.message_id, value)}
                  />
                ) : null}
              </div>
            );
          })
        )}
        {handoff === "offer" ? (
          <div className="help-chat-handoff">
            <button
              type="button"
              className="help-chat-handoff-btn"
              disabled={handoffBusy}
              onClick={() => void askForPerson()}
            >
              <UserRound size={14} aria-hidden="true" />
              Talk to a person
            </button>
          </div>
        ) : null}
        {handoff === "requested" ? (
          <p className="help-chat-handoff help-chat-handoff-sent" role="status">
            A member of the workshop team has been asked to help.
          </p>
        ) : null}
        <div ref={bottomRef} />
      </div>
      <div className="help-chat-composer">
        <input
          type="text"
          disabled={readOnly}
          value={draft}
          maxLength={2000}
          placeholder={readOnly ? "New input is disabled" : "Message the operators…"}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void send();
          }}
        />
        <button
          type="button"
          className="primary-btn"
          disabled={readOnly || busy || !draft.trim()}
          onClick={() => void send()}
          aria-label="Send"
        >
          <Send size={14} />
        </button>
      </div>
    </aside>
  );
}

function AnswerRating({
  vote,
  onRate,
}: {
  vote: VoteView;
  onRate: (value: HelpVote) => void;
}) {
  return (
    <div className="help-chat-rating" role="group" aria-label="Rate this answer">
      <button
        type="button"
        className="help-chat-rate"
        aria-label="Helpful"
        title="Helpful"
        aria-pressed={vote.chosen === "up"}
        disabled={vote.disabled}
        onClick={() => onRate("up")}
      >
        <ThumbsUp size={13} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="help-chat-rate"
        aria-label="Not helpful"
        title="Not helpful"
        aria-pressed={vote.chosen === "down"}
        disabled={vote.disabled}
        onClick={() => onRate("down")}
      >
        <ThumbsDown size={13} aria-hidden="true" />
      </button>
    </div>
  );
}
