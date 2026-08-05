// Run: node --test src/toasts.test.ts   (Node strips TS types natively)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MAX_TRANSIENT,
  MAX_TRANSIENT_MS,
  Toast,
  pushToast,
  transientTtlMs,
} from "./toasts.ts";

function toast(id: string, durability: Toast["durability"] = "transient"): Toast {
  return { id, level: "info", durability, title: id, body: id };
}

function fill(count: number, durability: Toast["durability"]): Toast[] {
  let stack: Toast[] = [];
  for (let i = 0; i < count; i += 1) {
    stack = pushToast(stack, toast(`${durability}:${i}`, durability));
  }
  return stack;
}

test("the same message pushed twice appears once", () => {
  const first = pushToast([], toast("help:1", "sticky"));
  const again = pushToast(first, toast("help:1", "sticky"));
  assert.equal(again.length, 1);
  assert.equal(again, first, "an ignored push should not produce a new array");
});

test("surplus pacing chatter drops oldest first", () => {
  const stack = fill(MAX_TRANSIENT + 2, "transient");
  assert.equal(stack.length, MAX_TRANSIENT);
  assert.equal(stack[0].id, "transient:2", "the two oldest should have gone");
});

test("an unread operator reply is never evicted to make room", () => {
  // Regression: the stack used to be capped as a whole, so a full stack of
  // sticky replies lost the earliest one — silently, while the operator's
  // console still showed it delivered.
  let stack = fill(6, "sticky");
  assert.equal(stack.length, 6);
  stack = pushToast(stack, toast("broadcast:noise"));
  assert.ok(
    stack.some((t) => t.id === "sticky:0"),
    "the first reply must still be on screen",
  );
});

test("chatter is dropped in preference to a critical notice", () => {
  let stack = fill(MAX_TRANSIENT, "transient");
  stack = pushToast(stack, toast("locked", "critical"));
  stack = pushToast(stack, toast("transient:late"));
  assert.ok(stack.some((t) => t.id === "locked"));
  assert.ok(!stack.some((t) => t.id === "transient:0"));
  assert.ok(stack.some((t) => t.id === "transient:late"));
});

test("a server ttl is honoured in milliseconds", () => {
  // Regression: every transient toast used the hardcoded 8s, so Control Tower's
  // default five-minute pacing notice vanished almost immediately.
  assert.equal(transientTtlMs(300), 300_000);
  assert.equal(transientTtlMs(12), 12_000);
});

test("a missing or nonsense ttl falls back to the default", () => {
  assert.equal(transientTtlMs(undefined), undefined);
  assert.equal(transientTtlMs(0), undefined);
  assert.equal(transientTtlMs(-5), undefined);
});

test("an absurd ttl cannot pin a transient toast on screen", () => {
  assert.equal(transientTtlMs(86_400), MAX_TRANSIENT_MS);
});
