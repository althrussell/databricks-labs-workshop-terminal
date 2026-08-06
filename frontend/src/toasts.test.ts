// Run: node --test src/toasts.test.ts   (Node strips TS types natively)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MAX_TRANSIENT,
  MAX_TRANSIENT_MS,
  Toast,
  clearHelpReplies,
  helpMessageSurface,
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

test("a reply toasts when the conversation is closed", () => {
  assert.deepEqual(helpMessageSurface({ senderRole: "operator", helpOpen: false }), {
    toast: true,
    acknowledge: true,
  });
});

test("a reply does not toast over the conversation it belongs to", () => {
  // The toast host sits above the composer, so toasting a reply the attendee is
  // already reading covers the box they would reply in.
  assert.equal(
    helpMessageSurface({ senderRole: "operator", helpOpen: true }).toast,
    false,
  );
});

test("a reply read in the open conversation still sends its receipt", () => {
  // The receipt used to be a side effect of the toast rendering. Suppressing
  // the toast without this would tell the operator "delivered, never opened"
  // about a reply the attendee had open on screen — and that signal is what
  // drives the attention queue.
  assert.equal(
    helpMessageSurface({ senderRole: "operator", helpOpen: true }).acknowledge,
    true,
  );
});

test("the attendee's own message neither toasts nor acknowledges", () => {
  assert.deepEqual(helpMessageSurface({ senderRole: "attendee", helpOpen: false }), {
    toast: false,
    acknowledge: false,
  });
});

test("a message Control Tower routed off the toast surface stays off it", () => {
  assert.equal(
    helpMessageSurface({ senderRole: "operator", helpOpen: false, surface: "banner" })
      .toast,
    false,
  );
});

test("clearing for the open conversation hands back what still needs receipting", () => {
  // The conversation displays these now, so they leave the stack — but the
  // receipt cannot leave with them. It used to ride on the toast rendering,
  // and anything cleared before that ran would be reported to the operator as
  // never opened while the attendee had it on screen.
  const stack = [
    { ...toast("help:1", "sticky"), ackMessageId: "m1" },
    toast("broadcast:pace"),
    { ...toast("help:2", "critical"), ackMessageId: "m2" },
  ];
  const { receipts, kept } = clearHelpReplies(stack);
  assert.deepEqual(receipts, ["m1", "m2"]);
  assert.deepEqual(
    kept.map((t) => t.id),
    ["broadcast:pace"],
  );
});

test("clearing leaves a reply that was never asked to be receipted", () => {
  const { receipts, kept } = clearHelpReplies([toast("help:1", "sticky")]);
  assert.deepEqual(receipts, []);
  assert.deepEqual(kept, []);
});

test("clearing a stack with no replies changes nothing", () => {
  const stack = [toast("broadcast:a"), toast("broadcast:b")];
  const { receipts, kept } = clearHelpReplies(stack);
  assert.deepEqual(receipts, []);
  assert.equal(kept.length, 2);
});

test("Control Tower can waive the receipt", () => {
  assert.equal(
    helpMessageSurface({ senderRole: "operator", helpOpen: true, requestAck: false })
      .acknowledge,
    false,
  );
});
