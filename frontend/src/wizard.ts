import type { WizardIdea } from "./api";

/** Title-case a schema slug. ``financial_services`` → ``Financial Services``. */
export function humanIndustry(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/** The schema a card belongs to, if it belongs to one.
 *
 * Tagged industries win; otherwise the schema of the first demo table. A
 * generic card (no tags, no tables) returns empty — picking it is a choice
 * not to steer the agent at a schema.
 */
export function industryOf(idea: WizardIdea): string {
  if (idea.industries.length > 0) return idea.industries[0];
  const first = idea.demo_tables[0];
  if (!first) return "";
  const schema = first.split(".")[0] ?? "";
  return schema;
}

/** Untagged cards that name no demo tables — they work with any data. */
export function isGenericIdea(idea: WizardIdea): boolean {
  return idea.industries.length === 0 && idea.demo_tables.length === 0;
}
