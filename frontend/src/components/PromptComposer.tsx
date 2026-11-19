import { useEffect, useImperativeHandle, useLayoutEffect, useRef, useState, type Ref, type RefObject } from "react";
import { Mic, MicOff, SendHorizontal, X } from "lucide-react";
import { pasteNotice, type ClipboardNotice } from "../terminalClipboard";
import { composeTranscript, startDictation, type Dictation, type RecognitionCtor } from "../voice";
import type { TerminalHandle } from "./TerminalView";

export interface PromptComposerHandle {
  /** Start listening, or stop if already listening. Call from a tap or click. */
  toggleListening: () => void;
}

interface Props {
  ref?: Ref<PromptComposerHandle>;
  terminal: RefObject<TerminalHandle | null>;
  /** The browser's recognizer; null hides the mic (Firefox, insecure pages). */
  speech: RecognitionCtor | null;
  /** Laptop: the composer opens from the agent bar and can be closed again. */
  onClose?: () => void;
}

/** About five lines; longer prompts scroll inside the box. */
const MAX_HEIGHT_PX = 128;
const NOTICE_MS = 8000;

/** A prompt box under the terminal: type or speak, read it back, then send.
 *
 * Typing into an agent TUI on a phone fights autocorrect and the soft keyboard;
 * a plain text box doesn't, and the keyboard's own dictation key works in it.
 * Send pastes the text into the agent and presses Enter, exactly as if the
 * attendee had typed it: nothing is stored or logged on the way.
 */
export default function PromptComposer({ ref, terminal, speech, onClose }: Props) {
  const [text, setText] = useState("");
  const [listening, setListening] = useState(false);
  // Shown beside the box rather than in the agent bar, which a phone hides
  // while its keyboard is open: a blocked microphone is never silent.
  const [notice, setNotice] = useState<ClipboardNotice | null>(null);
  const textRef = useRef(text);
  textRef.current = text;
  const dictationRef = useRef<Dictation | null>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);

  /** `discard`: the box was just sent or closed, so late results are dropped
   * instead of refilling it. Otherwise the recognizer's final words still land. */
  function stopListening(discard: boolean) {
    const current = dictationRef.current;
    if (!current) return;
    if (discard) {
      dictationRef.current = null;
      setListening(false);
    }
    current.stop();
  }

  function toggleListening() {
    if (!speech) return;
    if (dictationRef.current) {
      stopListening(false);
      return;
    }
    // Speech is added after whatever is already in the box.
    const base = textRef.current;
    let ended = false;
    let dictation: Dictation | null = null;
    setListening(true);
    dictation = startDictation(speech, {
      lang: navigator.language || "en-US",
      onText: (spoken) => {
        if (!ended && dictationRef.current === dictation) setText(composeTranscript(base, spoken));
      },
      onEnd: () => {
        ended = true;
        if (dictationRef.current === dictation) {
          dictationRef.current = null;
          setListening(false);
        }
      },
      onError: setNotice,
    });
    // A recognizer that refused to start has already ended.
    if (!ended) dictationRef.current = dictation;
  }

  useImperativeHandle(ref, () => ({ toggleListening }));

  // Stop the microphone when the composer goes away (session closed or ended).
  useEffect(() => () => dictationRef.current?.stop(), []);

  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [text]);

  function send() {
    if (!text.trim()) return;
    stopListening(true);
    if (terminal.current?.submit(text)) {
      setText("");
      setNotice(null);
    } else {
      setNotice(pasteNotice("offline"));
    }
  }

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  return (
    <>
    {notice && (
      <p className={`composer-notice clipboard-notice-${notice.level}`} role="status" aria-live="polite">
        {notice.text}
      </p>
    )}
    <form
      className="prompt-composer"
      aria-label="Prompt for the agent"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      {speech && (
        <button
          type="button"
          className={`composer-btn composer-mic${listening ? " composer-mic-live" : ""}`}
          aria-label={listening ? "Stop listening" : "Speak a prompt"}
          aria-pressed={listening}
          title={listening ? "Stop listening" : "Speak a prompt"}
          onClick={toggleListening}
        >
          {listening ? <MicOff size={18} aria-hidden="true" /> : <Mic size={18} aria-hidden="true" />}
        </button>
      )}
      <textarea
        ref={boxRef}
        className="composer-input"
        rows={1}
        value={text}
        placeholder={listening ? "Listening…" : "Prompt the agent"}
        aria-label="Prompt"
        enterKeyHint="send"
        autoCapitalize="sentences"
        maxLength={12000}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          // Shift+Enter adds a line; an IME's Enter confirms a word, not the prompt.
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            send();
          }
        }}
      />
      <button
        type="submit"
        className="composer-btn composer-send"
        aria-label="Send to the agent"
        title="Send to the agent"
        disabled={!text.trim()}
      >
        <SendHorizontal size={18} aria-hidden="true" />
      </button>
      {onClose && (
        <button
          type="button"
          className="icon-btn composer-close"
          aria-label="Close the prompt box"
          title="Close the prompt box"
          onClick={() => {
            stopListening(true);
            onClose();
          }}
        >
          <X size={14} aria-hidden="true" />
        </button>
      )}
    </form>
    </>
  );
}
