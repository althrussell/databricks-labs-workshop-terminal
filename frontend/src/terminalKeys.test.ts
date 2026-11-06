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
  assert.equal(decideTerminalKey(key({ ctrlKey: true }), true, true), "copy");
});

test("Ctrl+C with nothing selected still interrupts, as every terminal does", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true }), false), "terminal");
  assert.equal(decideTerminalKey(key({ ctrlKey: true }), false, true), "terminal");
});

test("Cmd+C and Cmd+V are left to the browser's native copy and paste on macOS", () => {
  assert.equal(decideTerminalKey(key({ metaKey: true }), true, true), "terminal");
  assert.equal(decideTerminalKey(key({ metaKey: true, key: "v" }), false, true), "terminal");
});

test("Ctrl+V pastes on Windows and Linux instead of sending ^V to the agent", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true, key: "v" }), false), "browser");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, key: "V" }), true), "browser");
});

test("Ctrl+V on macOS stays the control byte a Mac terminal sends", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true, key: "v" }), false, true), "terminal");
});

test("Ctrl+Shift+C copies even without a selection, so it never opens the inspector", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true, shiftKey: true }), true), "copy");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, shiftKey: true, key: "C" }), false), "copy");
});

test("Ctrl+Shift+V is the browser's plain-text paste on every platform", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true, shiftKey: true, key: "v" }), false), "browser");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, shiftKey: true, key: "V" }), false, true), "browser");
});

test("only keydown is decided; keyup and keypress pass through", () => {
  assert.equal(decideTerminalKey(key({ type: "keyup", ctrlKey: true }), true), "terminal");
  assert.equal(decideTerminalKey(key({ type: "keypress", ctrlKey: true }), true), "terminal");
  assert.equal(decideTerminalKey(key({ type: "keyup", ctrlKey: true, key: "v" }), false), "terminal");
});

test("other control chords reach the terminal untouched", () => {
  assert.equal(decideTerminalKey(key({ ctrlKey: true, key: "d" }), true), "terminal");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, altKey: true }), true), "terminal");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, altKey: true, key: "v" }), false), "terminal");
  assert.equal(decideTerminalKey(key({ ctrlKey: true, metaKey: true, key: "v" }), false), "terminal");
  assert.equal(decideTerminalKey(key({ key: "Enter" }), true), "terminal");
  assert.equal(decideTerminalKey(key({ key: "v" }), true), "terminal");
});
