import type { BuildRestore, ProjectInventory } from "./api";

export function buildName(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 63);
}

/** A local refresh must not hide copies the attendee explicitly discovered. */
export function keepSavedCopies(local: ProjectInventory, previous: ProjectInventory | null): ProjectInventory {
  const saved = new Set(previous?.projects.filter((item) => item.saved).map((item) => item.name));
  const names = new Set(local.projects.map((item) => item.name));
  return { ...local, projects: [
    ...local.projects.map((item) => ({ ...item, saved: item.saved || saved.has(item.name) })),
    ...(previous?.projects.filter((item) => item.saved && !names.has(item.name)) ?? []),
  ] };
}

export function goalChangePrompt(name: string, goal: string): string {
  return `I'd like to change the goal of the existing ${name} build to: ${goal}\n` +
    "Please read .workshop/project.json and the existing brief, keep my work, and update this project. " +
    "Use our workshop pacing: give a brief recommendation or ask one useful question if the change is unclear.";
}

/** A lost recovery response must not leave an error after confirmed completion.
 * Read the outcome once; never retry the source mutation. An already-selected
 * build is ambiguous, so it still requires the original recovery confirmation.
 */
export async function recoverWithConfirmation(
  name: string,
  previousActive: string | undefined,
  recover: (name: string) => Promise<ProjectInventory>,
  read: () => Promise<ProjectInventory>,
): Promise<ProjectInventory> {
  try { return await recover(name); }
  catch (caught) {
    if (!(caught instanceof TypeError) || previousActive === name) throw caught;
    let current: ProjectInventory;
    try { current = await read(); } catch { throw caught; }
    const source = current.projects.find((item) => item.name === name);
    // The backend selects a recovered build only after setup completes.
    if (current.active === name && source?.available && !source.missing) return current;
    throw caught;
  }
}

export function restoreInProgress(restore: BuildRestore | null | undefined): boolean {
  return restore?.state === "checking" || restore?.state === "restoring";
}

function names(items: string[]): string {
  return items.length <= 1 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** What the attendee is told, once, after their terminal restarted. */
export function restoreNotice(
  restore: BuildRestore | null | undefined,
  time: (epochSeconds: number) => string = (seconds) =>
    new Date(seconds * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }),
): string {
  if (!restore || restore.state === "checking") return "";
  if (restore.state === "restoring") return "Your terminal restarted. Restoring your saved builds…";
  const caveat = "Changes after that commit aren't included, and the agent's earlier conversation isn't either.";
  const parts: string[] = [];
  if (restore.active) {
    const when = restore.saved_at ? ` (${time(restore.saved_at)})` : "";
    parts.push(`Your terminal restarted. We restored ${restore.active} from your last saved commit${when}. ${caveat}`);
    const others = restore.restored.filter((name) => name !== restore.active);
    if (others.length) parts.push(`${names(others)} ${others.length === 1 ? "is" : "are"} back in My builds too.`);
  } else if (restore.restored.length) {
    parts.push(`Your terminal restarted. We restored ${names(restore.restored)} from your last saved ` +
      `${restore.restored.length === 1 ? "commit" : "commits"}; open My builds to continue. ${caveat}`);
  }
  if (restore.failed.length) {
    parts.push(`${parts.length ? "" : "Your terminal restarted. "}We couldn't restore ${names(restore.failed)} automatically. ` +
      "Open My builds and choose Recover source; nothing on this terminal was overwritten.");
  }
  return parts.join(" ");
}
