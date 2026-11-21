import assert from "node:assert/strict";
import test from "node:test";
import { isPhoneViewport, KeyboardTracker, measureViewport } from "./viewport.ts";

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
  assert.deepEqual(
    measureViewport(kb, { width: 195, height: 375, offsetTop: 120, scale: 2 }, 40, true),
    { appHeightPx: 750, keyboardOpen: false, resetScroll: false },
    "pinch-zoom must not shrink --app-height, mark keyboard open, or snap scrollTo(0, 0)"
  );
  // Layout viewport scrolled by iOS Safari even when vv.offsetTop === 0:
  assert.deepEqual(
    measureViewport(kb, { width: 390, height: 420, offsetTop: 0, scale: 1 }, 64, true),
    { appHeightPx: 420, keyboardOpen: true, resetScroll: true }
  );
});

test("a keyboard opened or closed while zoomed in still updates the layout", () => {
  const kb = new KeyboardTracker();
  measureViewport(kb, { width: 390, height: 750, offsetTop: 0, scale: 1 }, 0, true);
  const open = measureViewport(kb, { width: 195, height: 210, offsetTop: 90, scale: 2 }, 0, true);
  assert.deepEqual(open, { appHeightPx: 420, keyboardOpen: true, resetScroll: false });
  const closed = measureViewport(kb, { width: 195, height: 375, offsetTop: 90, scale: 2 }, 0, true);
  assert.deepEqual(closed, { appHeightPx: 750, keyboardOpen: false, resetScroll: false }, "no stale keyboard state");
});

test("opening the keyboard on a landscape tablet does not flip it into the phone shell", () => {
  const kb = new KeyboardTracker();
  const initial = measureViewport(kb, { width: 1024, height: 768, offsetTop: 0, scale: 1 }, 0, true);
  assert.equal(initial.keyboardOpen, false);
  assert.equal(isPhoneViewport(true, 1024, kb.fullHeight), false);

  // interactive-widget=resizes-content shrinks the viewport below 500px while typing.
  const typing = measureViewport(kb, { width: 1024, height: 418, offsetTop: 0, scale: 1 }, 0, true);
  assert.equal(typing.keyboardOpen, true);
  assert.equal(isPhoneViewport(true, 1024, kb.fullHeight), false, "fullHeight stays 768 so tablet keeps desktop shell");

  // Rotating a real phone to landscape changes width, resetting fullHeight to 380.
  const phoneLandscape = measureViewport(kb, { width: 814, height: 380, offsetTop: 0, scale: 1 }, 0, true);
  assert.equal(phoneLandscape.keyboardOpen, false);
  assert.equal(isPhoneViewport(true, 814, kb.fullHeight), true);

  // Narrow desktop browser without touch stays desktop.
  assert.equal(isPhoneViewport(false, 390, 844), false);
});
