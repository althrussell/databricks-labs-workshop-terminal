/** Who is speaking in the help conversation, and what the attendee can do next.
 *
 * Two voices answer an attendee: a person in the control room and Control
 * Tower's Workshop Assistant, an AI. An answer must never be mistaken for the
 * other kind, so the rules for telling them apart live here, where they can be
 * tested, rather than in the components that draw them.
 */

import type { HelpMessage, HelpVote } from "./api";

/** The name the assistant answers under. The AI badge travels with it. */
export const ASSISTANT_LABEL = "Workshop Assistant";

export type HelpBubble = "attendee" | "operator" | "assistant";

/** Roles whose messages are answers to the attendee rather than their own words. */
export function isReplyRole(role: string | undefined): boolean {
  return role === "operator" || role === "assistant";
}

export function isAssistantRole(role: string | undefined): boolean {
  return role === "assistant";
}

/** Bubble style. An unrecognised role draws as the attendee's own, as it always has. */
export function bubbleFor(role: string | undefined): HelpBubble {
  if (role === "assistant") return "assistant";
  if (role === "operator") return "operator";
  return "attendee";
}

/** The name above a message. The assistant's is fixed whatever it signed with. */
export function senderLabel(message: Pick<HelpMessage, "sender_role" | "sender">): string {
  if (message.sender_role === "assistant") return ASSISTANT_LABEL;
  if (message.sender_role === "operator") return message.sender || "Operator";
  return "You";
}

/** The title of the toast announcing a reply. */
export function replyToastTitle(role: string | undefined, sender: string | undefined): string {
  if (role === "assistant") return ASSISTANT_LABEL;
  return sender ? `${sender} replied` : "Operator replied";
}

/** The newest message the attendee did not write. */
export function latestReply(messages: HelpMessage[]): HelpMessage | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].sender_role !== "attendee") return messages[i];
  }
  return undefined;
}

export type HandoffView = "offer" | "requested" | "hidden";

/** Whether the conversation offers "Talk to a person", or says one is coming.
 *
 * Offered only while the assistant has the last word: once a person has
 * replied there is nobody else to ask for. `requestedAfter` is the assistant
 * answer the attendee asked a person about. The confirmation stands until
 * someone replies, and a newer assistant answer offers the choice again.
 */
export function handoffView(
  messages: HelpMessage[],
  { readOnly, requestedAfter }: { readOnly: boolean; requestedAfter: string | null },
): HandoffView {
  const reply = latestReply(messages);
  if (!reply) return "hidden";
  if (requestedAfter !== null && reply.message_id === requestedAfter) return "requested";
  if (readOnly || reply.sender_role !== "assistant") return "hidden";
  return "offer";
}

export interface VoteView {
  chosen: HelpVote | null;
  disabled: boolean;
}

/** One vote per answer: the buttons lock once it is cast, and while it is in flight. */
export function voteView(
  feedback: Partial<Record<string, HelpVote>>,
  messageId: string,
  { pending, readOnly }: { pending: boolean; readOnly: boolean },
): VoteView {
  const value = Object.prototype.hasOwnProperty.call(feedback, messageId)
    ? feedback[messageId]
    : undefined;
  const chosen = value === "up" || value === "down" ? value : null;
  return { chosen, disabled: chosen !== null || pending || readOnly };
}
