/** Speak a prompt instead of typing it, with the browser's own speech service.
 *
 * Chrome, Edge and Safari expose the Web Speech API; Firefox does not, so the
 * mic button only appears where it can work. Audio goes from the browser to
 * its vendor's speech service and never reaches WT: what comes back is text in
 * the prompt box, which the attendee reads, edits and sends like typed input.
 * A phone keyboard's own dictation key fills the same box without this code.
 *
 * Browser objects are passed in so the logic is testable without a browser.
 */

import type { ClipboardNotice } from "./terminalClipboard";

interface Alternative {
  transcript: string;
}

export interface RecognitionResultLike {
  readonly isFinal: boolean;
  readonly length: number;
  readonly [index: number]: Alternative;
}

export interface RecognitionEventLike {
  readonly results: { readonly length: number; readonly [index: number]: RecognitionResultLike };
}

export interface RecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: RecognitionEventLike) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort?(): void;
}

export type RecognitionCtor = new () => RecognitionLike;

interface SpeechWindow {
  isSecureContext?: boolean;
  SpeechRecognition?: unknown;
  webkitSpeechRecognition?: unknown;
}

/** The browser's recognizer, or null where voice input cannot work. */
export function speechRecognitionCtor(win: SpeechWindow): RecognitionCtor | null {
  // The microphone is only offered to secure pages (https, or localhost).
  if (!win.isSecureContext) return null;
  const ctor = win.SpeechRecognition ?? win.webkitSpeechRecognition;
  return typeof ctor === "function" ? (ctor as RecognitionCtor) : null;
}

/** Everything heard so far in one listening session, final and tentative alike. */
export function spokenText(results: RecognitionEventLike["results"], cumulative = false): string {
  const parts: string[] = [];
  for (let i = 0; i < results.length; i += 1) {
    const text = results[i]?.[0]?.transcript?.trim();
    if (!text) continue;
    const prev = parts[parts.length - 1];
    // Only Android repeats what came before; elsewhere a repeat is speech
    // ("no, no"), so every result is kept.
    if (cumulative && prev && restates(prev, text)) {
      parts[parts.length - 1] = text;
      continue;
    }
    parts.push(text);
  }
  return parts.join(" ");
}

/** Android Chrome's recognizer, with `continuous` on, starts each new result
 * with the whole utterance so far ("build", "build a", "build a sales chart"),
 * revising earlier words as it goes. Other browsers give separate segments. */
export function cumulativeResults(userAgent: string): boolean {
  return /\bAndroid\b/i.test(userAgent);
}

const words = (text: string) => text.toLowerCase().split(/\s+/).filter(Boolean);

/** Whether `cur` restates `prev`: at least half of prev's words recur in the
 * same places, and it is no more than a word shorter. A revised word anywhere
 * ("add the chart" to "add a chart") still counts as the same utterance. */
function restates(prev: string, cur: string): boolean {
  const before = words(prev);
  const after = words(cur);
  if (after.length + 1 < before.length) return false;
  const same = before.filter((word, i) => after[i] === word).length;
  return same >= Math.ceil(before.length / 2);
}

/** The prompt box while listening: what was typed before, then what is heard. */
export function composeTranscript(base: string, spoken: string): string {
  const heard = spoken.replace(/\s+/g, " ").trim();
  if (!heard) return base;
  if (!base) return heard;
  return /\s$/.test(base) ? base + heard : `${base} ${heard}`;
}

/** What to tell the attendee when listening stops on an error; null for none. */
export function voiceNotice(error: string): ClipboardNotice | null {
  switch (error) {
    case "no-speech":
    case "aborted":
      return null;
    case "not-allowed":
    case "service-not-allowed":
      return { level: "warning", text: "Microphone blocked. Allow it for this site in your browser settings." };
    case "audio-capture":
      return { level: "warning", text: "No microphone found." };
    case "network":
      return { level: "warning", text: "The browser's speech service can't be reached. Type your prompt instead." };
    case "language-not-supported":
      return { level: "info", text: "Voice input doesn't support your browser's language." };
    default:
      return { level: "warning", text: "Voice input stopped. Try again, or type your prompt." };
  }
}

export interface DictationHandlers {
  lang: string;
  /** The recognizer restates earlier results (Android Chrome): see cumulativeResults. */
  cumulative?: boolean;
  /** Called with everything heard so far each time the recognizer updates. */
  onText: (spoken: string) => void;
  /** Called once when listening ends for any reason. */
  onEnd: () => void;
  onError: (notice: ClipboardNotice) => void;
}

export interface Dictation {
  stop: () => void;
  abort: () => void;
}

export function startDictation(Ctor: RecognitionCtor, handlers: DictationHandlers): Dictation {
  const recognition = new Ctor();
  let ended = false;
  const finish = () => {
    if (ended) return;
    ended = true;
    handlers.onEnd();
  };
  recognition.lang = handlers.lang;
  // Keep listening through short pauses; Safari may still end early, and the
  // box keeps whatever was heard.
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.onresult = (event) => {
    if (!ended) handlers.onText(spokenText(event.results, handlers.cumulative ?? false));
  };
  recognition.onerror = (event) => {
    const notice = voiceNotice(event.error);
    if (notice) handlers.onError(notice);
  };
  recognition.onend = finish;
  try {
    recognition.start();
  } catch {
    // start() throws if a recognizer is already running on this page.
    ended = true;
    handlers.onError({ level: "warning", text: "Voice input is busy. Try again in a moment." });
    handlers.onEnd();
  }
  return {
    stop: () => recognition.stop(),
    abort: () => {
      if (recognition.abort) recognition.abort();
      else recognition.stop();
      finish();
    },
  };
}
