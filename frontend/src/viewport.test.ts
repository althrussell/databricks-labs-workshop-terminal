import assert from "node:assert/strict";
import test from "node:test";
import { KeyboardTracker } from "./viewport.ts";

test("a soft keyboard taking a large share of the height reads as open", () => {
  const kb = new KeyboardTracker();
  assert.equal(kb.update(390, 750), false);
  assert.equal(kb.update(390, 700), false, "browser toolbars sliding in are not a keyboard");
  assert.equal(kb.update(390, 420), true);
  assert.equal(kb.update(390, 750), false);
});

test("rotating the phone resets what full height means", () => {
  const kb = new KeyboardTracker();
  kb.update(390, 750);
  assert.equal(kb.update(844, 340), false, "landscape is short, not keyboard-covered");
  assert.equal(kb.update(844, 180), true);
  assert.equal(kb.update(390, 750), false);
});
