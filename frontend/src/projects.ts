import type { ProjectInventory } from "./api";

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
