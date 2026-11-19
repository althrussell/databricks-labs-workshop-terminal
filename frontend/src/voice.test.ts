import assert from "node:assert/strict";
import test from "node:test";
import {
  composeTranscript,
  speechRecognitionCtor,
  spokenText,
  cumulativeResults,
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
  aborted = false;
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
  abort() {
    this.aborted = true;
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

test("cumulative Android results collapse without eating distinct short words", () => {
  assert.equal(
    spokenText(results(["build", true], ["build a", true], ["build a sales", true], ["build a sales chart", false]), true),
    "build a sales chart"
  );
  assert.equal(spokenText(results(["hello", true], ["hello", true]), true), "hello");
  assert.equal(spokenText(results(["no", true], ["not yet", false]), true), "no not yet");
});

test("an Android revision of an earlier word replaces it instead of repeating the sentence", () => {
  assert.equal(spokenText(results(["build a sale", true], ["build a sales chart", false]), true), "build a sales chart");
  assert.equal(spokenText(results(["add the chart", true], ["add a chart", false]), true), "add a chart");
  assert.equal(
    spokenText(results(["make it blue", true], ["make it blue and add a legend", false]), true),
    "make it blue and add a legend"
  );
});

test("other browsers keep every segment, including a phrase said twice", () => {
  assert.equal(spokenText(results(["no", true], ["no", true])), "no no");
  assert.equal(spokenText(results(["build a sale", true], ["build a sales chart", false])), "build a sale build a sales chart");
});

test("only Android user agents restate earlier results", () => {
  assert.equal(cumulativeResults("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"), true);
  assert.equal(cumulativeResults("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1"), false);
  assert.equal(cumulativeResults("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"), false);
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

test("aborting dictation cancels recognition and fires onEnd immediately even if onend stalls", () => {
  FakeRecognition.failStart = false;
  let ends = 0;
  const dictation = startDictation(FakeRecognition, {
    lang: "en",
    onText: () => {},
    onEnd: () => (ends += 1),
    onError: () => {},
  });
  const rec = FakeRecognition.last!;
  dictation.abort();
  assert.ok(rec.aborted);
  assert.equal(ends, 1);
  rec.onend?.();
  assert.equal(ends, 1, "late browser onend does not fire onEnd twice");
});
