// What to tell an attendee about the tab their sign-in lives in.
//
// Workshop Terminal never holds a refresh token: the Apps proxy hands it a
// short-lived access token on each request the attendee's tab makes, and the
// Omnigent host in their container runs off a mirror of it. Close the tab long
// enough and every Omnigent agent fails with an error that says nothing about
// tabs. So the rule is stated on screen for as long as it applies, and stated
// unmissably the moment it is broken.
//
// Kept apart from the component so the wording and the thresholds can be tested
// as facts rather than through a rendered tree.

import type { OboStatus } from "./api";

/** Minutes of remaining sign-in below which the quiet notice starts warning. */
export const SOON_MINUTES = 10;

export type SignInNoticeState =
  | { kind: "none" }
  | { kind: "rule"; minutes: number | null; soon: boolean }
  | { kind: "expired" };

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
  if (!obo.present) return { kind: "rule", minutes: null, soon: false };
  const minutes = obo.expires_in == null ? null : Math.round(obo.expires_in / 60);
  if (!obo.fresh || (minutes !== null && minutes <= 0)) return { kind: "expired" };
  return { kind: "rule", minutes, soon: minutes !== null && minutes <= SOON_MINUTES };
}
