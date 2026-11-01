import assert from "node:assert/strict";
import { test } from "node:test";
import type { HelpMessage } from "./api.ts";
import {
  ASSISTANT_LABEL,
  bubbleFor,
  handoffView,
  isReplyRole,
  latestReply,
  replyToastTitle,
  senderLabel,
  voteView,
} from "./helpChat.ts";

function message(id: string, sender_role: string, sender = ""): HelpMessage {
  return { message_id: id, sender_role, sender, body: id, created_at: "2026-11-01T02:00:00Z" };
}

const question = message("q1", "attendee");
const answer = message("a1", "assistant", "Workshop Assistant");
const person = message("o1", "operator", "Sam");

test("an assistant answer is labelled as the assistant, whatever it signed with", () => {
  // The legacy signature carries "(AI)" for terminals that cannot badge it;
  // here the badge does that job, so the label stays the plain name.
  assert.equal(senderLabel(message("a", "assistant", "Workshop Assistant (AI)")), ASSISTANT_LABEL);
  assert.equal(senderLabel(message("a", "assistant")), "Workshop Assistant");
});

test("operator and attendee labels are unchanged", () => {
  assert.equal(senderLabel(person), "Sam");
  assert.equal(senderLabel(message("o", "operator")), "Operator");
  assert.equal(senderLabel(question), "You");
  assert.equal(senderLabel(message("x", "someone-new")), "You");
});

test("each voice has its own bubble, and an unknown role draws as before", () => {
  assert.equal(bubbleFor("assistant"), "assistant");
  assert.equal(bubbleFor("operator"), "operator");
  assert.equal(bubbleFor("attendee"), "attendee");
  assert.equal(bubbleFor("someone-new"), "attendee");
  assert.equal(bubbleFor(undefined), "attendee");
});

test("assistant answers count as replies, the attendee's own words do not", () => {
  assert.equal(isReplyRole("operator"), true);
  assert.equal(isReplyRole("assistant"), true);
  assert.equal(isReplyRole("attendee"), false);
  assert.equal(isReplyRole(undefined), false);
});

test("a reply toast names the assistant plainly and keeps the operator wording", () => {
  assert.equal(replyToastTitle("assistant", "Workshop Assistant (AI)"), "Workshop Assistant");
  assert.equal(replyToastTitle("operator", "Sam"), "Sam replied");
  assert.equal(replyToastTitle("operator", ""), "Operator replied");
});

test("the latest reply skips what the attendee wrote since", () => {
  assert.equal(latestReply([question, answer, message("q2", "attendee")]), answer);
  assert.equal(latestReply([question]), undefined);
  assert.equal(latestReply([]), undefined);
});

test("a person is offered only while the assistant has the last word", () => {
  const open = { readOnly: false, requestedAfter: null };
  assert.equal(handoffView([], open), "hidden");
  assert.equal(handoffView([question], open), "hidden");
  assert.equal(handoffView([question, answer], open), "offer");
  // Answering the assistant back does not take the choice away.
  assert.equal(handoffView([question, answer, message("q2", "attendee")], open), "offer");
  // Once a person has replied there is nobody else to ask for.
  assert.equal(handoffView([question, answer, person], open), "hidden");
});

test("a closed workshop offers no handoff", () => {
  assert.equal(handoffView([question, answer], { readOnly: true, requestedAfter: null }), "hidden");
});

test("asking for a person replaces the button with the confirmation until someone replies", () => {
  const asked = { readOnly: false, requestedAfter: answer.message_id };
  assert.equal(handoffView([question, answer], asked), "requested");
  assert.equal(handoffView([question, answer, message("q2", "attendee")], asked), "requested");
  assert.equal(handoffView([question, answer, person], asked), "hidden");
  // A newer assistant answer is a new chance to ask.
  assert.equal(handoffView([question, answer, message("a2", "assistant")], asked), "offer");
});

test("an answer can be rated until a vote is cast, then shows the vote", () => {
  const idle = { pending: false, readOnly: false };
  assert.deepEqual(voteView({}, "a1", idle), { chosen: null, disabled: false });
  assert.deepEqual(voteView({ a1: "up" }, "a1", idle), { chosen: "up", disabled: true });
  assert.deepEqual(voteView({ a1: "down" }, "a1", idle), { chosen: "down", disabled: true });
  assert.deepEqual(voteView({ a2: "up" }, "a1", idle), { chosen: null, disabled: false });
});

test("the buttons lock while a vote is in flight and when the workshop is closed", () => {
  assert.deepEqual(voteView({}, "a1", { pending: true, readOnly: false }), { chosen: null, disabled: true });
  assert.deepEqual(voteView({}, "a1", { pending: false, readOnly: true }), { chosen: null, disabled: true });
});

test("an id that names an object property is not mistaken for a vote", () => {
  const idle = { pending: false, readOnly: false };
  assert.deepEqual(voteView({}, "constructor", idle), { chosen: null, disabled: false });
  assert.deepEqual(voteView({}, "__proto__", idle), { chosen: null, disabled: false });
});
