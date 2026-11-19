import assert from "node:assert/strict";
import test from "node:test";
import {
  composeTranscript,
  speechRecognitionCtor,
  spokenText,
  startDictation,
  voiceNotice,
  type RecognitionEventLike,
  type RecognitionLike,
} from "./voice.ts";

class FakeRecognition implements RecognitionLike {
  static last: FakeRecognition | null = null;
  static failStart = false;
  lang = "";
  continuous = false;
  interimResults = false;
  onresult: RecognitionLike["onresult"] = null;
  onerror: RecognitionLike["onerror"] = null;
  onend: RecognitionLike["onend"] = null;
  started = false;
  stopped = false;
  constructor() {
    FakeRecognition.last = this;
  }
  start() {
    if (FakeRecognition.failStart) throw new Error("InvalidStateError");
    this.started = true;
  }
  stop() {
    this.stopped = true;
  }
}

const results = (...items: [string, boolean][]): RecognitionEventLike["results"] =>
  Object.assign(
    items.map(([transcript, isFinal]) => Object.assign([{ transcript }], { isFinal })),
    { length: items.length }
  );

test("voice input is offered only on secure pages with a recognizer", () => {
  assert.equal(speechRecognitionCtor({ isSecureContext: true, webkitSpeechRecognition: FakeRecognition }), FakeRecognition);
  assert.equal(speechRecognitionCtor({ isSecureContext: true, SpeechRecognition: FakeRecognition }), FakeRecognition);
  assert.equal(speechRecognitionCtor({ isSecureContext: false, SpeechRecognition: FakeRecognition }), null);
  assert.equal(speechRecognitionCtor({ isSecureContext: true }), null, "Firefox has no recognizer");
});

test("final and tentative results read as one sentence", () => {
  assert.equal(spokenText(results(["build a dashboard", true], [" for sales ", false])), "build a dashboard for sales");
  assert.equal(spokenText(results()), "");
});

test("speech is appended to what the attendee already typed", () => {
  assert.equal(composeTranscript("", "add a chart"), "add a chart");
  assert.equal(composeTranscript("Fix the bug.", "Then  add a test"), "Fix the bug. Then add a test");
  assert.equal(composeTranscript("Fix: ", "the header"), "Fix: the header");
  assert.equal(composeTranscript("keep me", "   "), "keep me");
});

test("only errors the attendee can act on produce a notice", () => {
  assert.equal(voiceNotice("no-speech"), null);
  assert.equal(voiceNotice("aborted"), null);
  assert.equal(voiceNotice("not-allowed")?.level, "warning");
  assert.equal(voiceNotice("audio-capture")?.text, "No microphone found.");
  assert.equal(voiceNotice("something-new")?.level, "warning");
});

test("dictation streams text, reports errors and ends exactly once", () => {
  FakeRecognition.failStart = false;
  const heard: string[] = [];
  const errors: string[] = [];
  let ends = 0;
  const dictation = startDictation(FakeRecognition, {
    lang: "en-AU",
    onText: (t) => heard.push(t),
    onEnd: () => (ends += 1),
    onError: (n) => errors.push(n.text),
  });
  const rec = FakeRecognition.last!;
  assert.ok(rec.started && rec.continuous && rec.interimResults);
  assert.equal(rec.lang, "en-AU");
  rec.onresult?.({ results: results(["hello", false]) });
  rec.onresult?.({ results: results(["hello world", true]) });
  rec.onerror?.({ error: "not-allowed" });
  rec.onend?.();
  rec.onend?.();
  dictation.stop();
  assert.deepEqual(heard, ["hello", "hello world"]);
  assert.equal(errors.length, 1);
  assert.equal(ends, 1);
  assert.ok(rec.stopped);
});

test("a recognizer that refuses to start ends with a notice", () => {
  FakeRecognition.failStart = true;
  const errors: string[] = [];
  let ends = 0;
  startDictation(FakeRecognition, {
    lang: "en",
    onText: () => {},
    onEnd: () => (ends += 1),
    onError: (n) => errors.push(n.text),
  });
  FakeRecognition.failStart = false;
  assert.equal(ends, 1);
  assert.equal(errors.length, 1);
});
