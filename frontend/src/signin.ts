// What to tell an attendee about the tab their sign-in lives in.
//
// Workshop Terminal never holds a refresh token: the Apps proxy hands it a
// short-lived access token on each request the attendee's tab makes, and the
// Omnigent host in their container runs off a mirror of it. Close the tab long
// enough and every Omnigent agent fails with an error that says nothing about
// tabs. So the rule is stated on screen for as long as it applies, and stated
// unmissably the moment it is broken.
//
// Kept apart from the component so which notice applies can be tested as a fact
// rather than through a rendered tree.
//
// No time remaining is shown. A countdown that renews on its own is not
// something an attendee can act on, and watching it tick reads as a failure in
// progress; the rule holds for the whole event either way.

import type { OboStatus } from "./api";

export type SignInNoticeState = { kind: "none" } | { kind: "rule" } | { kind: "expired" };

/**
 * Which notice belongs on screen, if any.
 *
 * Silent on deployments with no Omnigent plane and when OBO is switched off:
 * there, closing the tab costs nothing, and a warning about a consequence that
 * cannot happen is noise that teaches attendees to ignore the strip.
 */
export function signInNotice(
  omnigentEnabled: boolean,
  obo: OboStatus | undefined
): SignInNoticeState {
  if (!omnigentEnabled || !obo?.enabled) return { kind: "none" };
  // Never captured is not expired. A tab that has not yet handed over a token
  // is the normal state for the first second of the event, and telling someone
  // their sign-in expired before they have one reads as a broken product.
  if (!obo.present) return { kind: "rule" };
  if (!obo.fresh || (obo.expires_in !== null && obo.expires_in <= 0)) return { kind: "expired" };
  return { kind: "rule" };
}
