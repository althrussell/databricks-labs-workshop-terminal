import assert from "node:assert/strict";
import test from "node:test";
import { keySequence, TOUCH_KEYS } from "./touchKeys.ts";

test("menu keys send the bytes a hardware keyboard would", () => {
  assert.equal(keySequence("esc", false), "\x1b");
  assert.equal(keySequence("tab", false), "\t");
  assert.equal(keySequence("shiftTab", false), "\x1b[Z");
  assert.equal(keySequence("enter", false), "\r");
  assert.equal(keySequence("ctrlC", false), "\x03");
});

test("arrows follow the terminal's cursor-key mode", () => {
  assert.deepEqual(
    ["up", "down", "right", "left"].map((k) => keySequence(k as "up", false)),
    ["\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"]
  );
  assert.deepEqual(
    ["up", "down", "right", "left"].map((k) => keySequence(k as "up", true)),
    ["\x1bOA", "\x1bOB", "\x1bOC", "\x1bOD"]
  );
});

test("every key on the bar has a label and an accessible name", () => {
  const keys = TOUCH_KEYS.map((k) => k.key);
  assert.equal(new Set(keys).size, keys.length);
  for (const k of TOUCH_KEYS) {
    assert.ok(k.label && k.name);
    assert.ok(keySequence(k.key, false).length > 0);
  }
});
