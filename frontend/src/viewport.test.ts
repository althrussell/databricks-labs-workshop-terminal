import assert from "node:assert/strict";
import test from "node:test";
import { KeyboardTracker, measureViewport } from "./viewport.ts";

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

test("pinch-zoom leaves app height and pan alone while unzoomed offset or scroll resets", () => {
  const kb = new KeyboardTracker();
  assert.deepEqual(
    measureViewport(kb, { width: 390, height: 750, offsetTop: 0, scale: 1 }, 0, true),
    { appHeightPx: 750, keyboardOpen: false, resetScroll: false }
  );
  // Pinch-zoomed to 2x: visualViewport shrinks to 195x375 with offsetTop > 0.
  assert.equal(
    measureViewport(kb, { width: 195, height: 375, offsetTop: 120, scale: 2 }, 40, true),
    null,
    "pinch-zoom must not shrink --app-height, mark keyboard open, or snap scrollTo(0, 0)"
  );
  // Layout viewport scrolled by iOS Safari even when vv.offsetTop === 0:
  assert.deepEqual(
    measureViewport(kb, { width: 390, height: 420, offsetTop: 0, scale: 1 }, 64, true),
    { appHeightPx: 420, keyboardOpen: true, resetScroll: true }
  );
});
