import type { AttendeeProfile, ExplanationDetail, ProfileSave, WorkingApproach } from "./api.ts";

export type Vocabulary = "" | "guided" | "technical";

export interface Choices {
  approach: WorkingApproach;
  detail: ExplanationDetail;
  vocabulary: Vocabulary;
}

/** The choices a saved profile shows, with the legacy single preference migrated (W2). */
export function choicesFrom(profile: AttendeeProfile): Choices {
  const legacy = profile.help_preference;
  return {
    // Unset defaults gently to building together, which is what the agent is told.
    approach: profile.working_approach || "co_build",
    // "Keep it concise" was a detail level all along.
    detail: profile.explanation_detail || (legacy === "concise" ? "brief" : ""),
    vocabulary: legacy === "technical" || legacy === "guided" ? legacy : "",
  };
}

/** Only the changed dimensions are sent; the rest of the profile and the task stay as they are. */
export function changesFrom(profile: AttendeeProfile, choices: Choices): ProfileSave {
  const saved = choicesFrom(profile);
  const body: ProfileSave = { expected_revision: profile.revision };
  if (choices.approach !== saved.approach) body.working_approach = choices.approach;
  if (choices.detail !== saved.detail || (profile.help_preference === "concise" && !profile.explanation_detail)) {
    body.explanation_detail = choices.detail;
  }
  if (choices.vocabulary !== saved.vocabulary || profile.help_preference === "concise") {
    body.help_preference = choices.vocabulary;
  }
  return body;
}

