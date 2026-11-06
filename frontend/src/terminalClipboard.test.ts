import assert from "node:assert/strict";
import test from "node:test";
import {
  copyNotice,
  copyText,
  isMacPlatform,
  pasteIsImageOnly,
  pasteNotice,
  readClipboardText,
  type ClipboardEnv,
} from "./terminalClipboard.ts";

function env(overrides: Partial<ClipboardEnv> & { legacy?: boolean } = {}) {
  const calls = { write: [] as string[], legacy: [] as string[] };
  const result: ClipboardEnv = {
    secure: true,
    clipboard: {
      writeText: async (text) => {
        calls.write.push(text);
      },
      readText: async () => "echo hello",
    },
    legacyCopy: (text) => {
      calls.legacy.push(text);
      return overrides.legacy ?? true;
    },
    ...overrides,
  };
  return { env: result, calls };
}

const denied = async () => {
  throw new DOMException("denied", "NotAllowedError");
};

test("copy uses the async Clipboard API when the browser allows it", async () => {
  const { env: e, calls } = env();
  assert.equal(await copyText("SELECT 1", e), "copied");
  assert.deepEqual(calls.write, ["SELECT 1"]);
  assert.deepEqual(calls.legacy, []);
});

test("a refused async copy falls back to execCommand instead of failing silently", async () => {
  const { env: e, calls } = env({ clipboard: { writeText: denied } });
  assert.equal(await copyText("SELECT 1", e), "copied");
  assert.deepEqual(calls.legacy, ["SELECT 1"]);
});

test("an insecure context or missing API goes straight to the fallback", async () => {
  const insecure = env({ secure: false });
  assert.equal(await copyText("x", insecure.env), "copied");
  assert.deepEqual(insecure.calls.write, []);
  assert.deepEqual(insecure.calls.legacy, ["x"]);

  const missing = env({ clipboard: undefined });
  assert.equal(await copyText("x", missing.env), "copied");
  assert.deepEqual(missing.calls.legacy, ["x"]);
});

test("copy reports blocked only when every path was refused", async () => {
  const { env: e } = env({ clipboard: { writeText: denied }, legacy: false });
  assert.equal(await copyText("x", e), "blocked");
});

test("copying an empty selection touches no clipboard", async () => {
  const { env: e, calls } = env();
  assert.equal(await copyText("", e), "empty");
  assert.deepEqual(calls, { write: [], legacy: [] });
});

test("reading the clipboard returns its text, multiline intact", async () => {
  const { env: e } = env({ clipboard: { readText: async () => "line one\nline two\n" } });
  assert.deepEqual(await readClipboardText(e), { kind: "text", text: "line one\nline two\n" });
});

test("a denied, unavailable or insecure read is blocked, which opens the paste box", async () => {
  assert.deepEqual(await readClipboardText(env({ clipboard: { readText: denied } }).env), { kind: "blocked" });
  assert.deepEqual(await readClipboardText(env({ clipboard: {} }).env), { kind: "blocked" });
  assert.deepEqual(await readClipboardText(env({ clipboard: undefined }).env), { kind: "blocked" });
  assert.deepEqual(await readClipboardText(env({ secure: false }).env), { kind: "blocked" });
});

test("an image-only clipboard reads as empty, not as a paste of nothing", async () => {
  assert.deepEqual(await readClipboardText(env({ clipboard: { readText: async () => "" } }).env), { kind: "empty" });
});

test("a native paste carrying only a screenshot is recognised", () => {
  const data = (text: string, types: string[]) => ({ getData: () => text, types });
  assert.equal(pasteIsImageOnly(data("", ["Files"])), true);
  assert.equal(pasteIsImageOnly(data("hi", ["text/plain", "Files"])), false);
  assert.equal(pasteIsImageOnly(data("", ["text/html"])), false);
  assert.equal(pasteIsImageOnly(null), false);
});

test("every copy outcome tells the attendee what happened or what to do", () => {
  assert.equal(copyNotice("copied", false).level, "success");
  assert.match(copyNotice("empty", false).text, /hold Shift/);
  assert.match(copyNotice("empty", true).text, /⌥ Option/);
  assert.equal(copyNotice("blocked", false).level, "warning");
  assert.match(copyNotice("blocked", false).text, /right-click it and choose Copy/);
  assert.match(copyNotice("blocked", true).text, /⌘C/);
});

test("every paste outcome has a message, and a paste never claims it was sent", () => {
  for (const outcome of ["pasted", "empty", "offline", "image"] as const) {
    assert.ok(pasteNotice(outcome).text.length > 0);
  }
  assert.match(pasteNotice("pasted").text, /Press Enter/);
  assert.match(pasteNotice("image").text, /Attach image/);
  assert.equal(pasteNotice("offline").level, "warning");
});

test("platform detection prefers userAgentData and recognises Apple devices", () => {
  assert.equal(isMacPlatform({ userAgentData: { platform: "macOS" } }), true);
  assert.equal(isMacPlatform({ platform: "MacIntel" }), true);
  assert.equal(isMacPlatform({ userAgent: "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)" }), true);
  assert.equal(isMacPlatform({ userAgentData: { platform: "Windows" }, platform: "Win32" }), false);
  assert.equal(isMacPlatform({ platform: "Linux x86_64" }), false);
  assert.equal(isMacPlatform(undefined), false);
});
