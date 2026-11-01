import assert from "node:assert/strict";
import test from "node:test";
import { decideTerminalKey } from "./terminalKeys.ts";

const key = (overrides: Partial<Parameters<typeof decideTerminalKey>[0]>) => ({
  type: "keydown",
  key: "c",
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  ...overrides,
});

test("Ctrl+C with a selection copies instead of interrupting the agent", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true }), true), "copy");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, key: "C" }), true), "copy");
});

test("Ctrl+C with nothing selected still interrupts, as every terminal does", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true }), false), "terminal");
});

test("Cmd+C is left to the browser's native copy on macOS", () => {
  assert.equal(decideTerminalKey(key({ metaKey: true }), true), "terminal");
});

test("Ctrl+Shift+C/V are browser copy/paste chords, never control bytes", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true, shiftKey: true }), false), "browser");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, shiftKey: true, key: "v" }), false), "browser");
});

test("only keydown is decided; keyup and keypress pass through", () => {
  assert.equal(decideTerminalKey(key({ type: "keyup", ctrlKey: true }), true), "terminal");
  assert.equal(decideTerminalKey(key({ type: "keypress", ctrlKey: true }), true), "terminal");
});

test("other control chords reach the terminal untouched", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true, key: "d" }), true), "terminal");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, altKey: true }), true), "terminal");
  assert.equal(decideTerminalKey(key({ key: "Enter" }), true), "terminal");
});
